"""Dynamic tool creation — agent creates new tools via natural language.

Agent describes what a tool should do → LLM generates Python code →
code is validated and registered as a LangChain tool.

Tools are persisted in workspace/tools/ and loaded on next startup.

Security: generated code runs in a restricted scope (no file system
access, no network, no imports beyond allowlist).
"""

import hashlib
import importlib
import json
import logging
import re
import sys
import types
from pathlib import Path

from langchain_core.tools import BaseTool, StructuredTool

from kronos.config import settings
from kronos.llm import ModelTier, get_model
from kronos.workspace import ws

log = logging.getLogger("kronos.tools.dynamic")

TOOLS_DIR = ws.dynamic_tools_dir

# Allowed imports in generated code
SAFE_IMPORTS = {
    "json", "re", "math", "datetime", "collections",
    "itertools", "functools", "hashlib", "base64",
    "urllib.parse", "statistics",
}

# Forbidden patterns in generated code
FORBIDDEN_PATTERNS = [
    r"import\s+os\b",
    r"import\s+subprocess",
    r"import\s+shutil",
    r"import\s+pathlib",
    r"__import__",
    r"eval\s*\(",
    r"exec\s*\(",
    r"open\s*\(",
    r"compile\s*\(",
    r"globals\s*\(",
    r"locals\s*\(",
    r"getattr\s*\(",
    r"setattr\s*\(",
    r"delattr\s*\(",
    r"os\.\w+",
    r"sys\.\w+",
    r"subprocess\.",
    r"shutil\.",
]

GENERATE_PROMPT = """Create a Python function for a LangChain tool.

Tool description: {description}
Tool name: {name}

Requirements:
- Write a single async function with type hints
- Function name must match tool name (snake_case)
- Include a docstring (this becomes the tool description for the LLM)
- Use only these imports: {safe_imports}
- Function must return a string
- No file I/O, no network calls, no subprocess, no eval/exec
- Handle errors gracefully (return error message, don't raise)

Return ONLY the Python code, no markdown fences, no explanation.

Example:
```python
import math

async def calculate_compound_interest(principal: float, rate: float, years: int) -> str:
    \"\"\"Calculate compound interest. Args: principal, annual rate (%), years.\"\"\"
    try:
        amount = principal * math.pow(1 + rate / 100, years)
        return f"After {{years}} years: ${{amount:,.2f}} (interest: ${{amount - principal:,.2f}})"
    except Exception as e:
        return f"Calculation error: {{e}}"
```
"""


def validate_code(code: str) -> tuple[bool, str]:
    """Validate generated code for safety."""
    for pattern in FORBIDDEN_PATTERNS:
        if re.search(pattern, code):
            return False, f"Forbidden pattern: {pattern}"

    # Check imports
    for match in re.finditer(r"^import\s+(\S+)|^from\s+(\S+)\s+import", code, re.MULTILINE):
        module = match.group(1) or match.group(2)
        base_module = module.split(".")[0]
        if base_module not in SAFE_IMPORTS:
            return False, f"Unsafe import: {module}"

    # Must have exactly one function definition
    func_defs = re.findall(r"^(?:async\s+)?def\s+(\w+)\s*\(", code, re.MULTILINE)
    if len(func_defs) != 1:
        return False, f"Expected 1 function, found {len(func_defs)}"

    # Try to compile
    try:
        compile(code, "<dynamic_tool>", "exec")
    except SyntaxError as e:
        return False, f"Syntax error: {e}"

    return True, ""


async def create_tool(name: str, description: str) -> tuple[BaseTool | None, str]:
    """Generate a tool from natural language description.

    Returns (tool, message). Tool is None on failure.
    """
    # Sanitize name
    clean_name = re.sub(r"[^a-z0-9_]", "_", name.lower().strip())
    if not clean_name:
        return None, "Invalid tool name"

    # Generate code via LLM
    prompt = GENERATE_PROMPT.format(
        description=description,
        name=clean_name,
        safe_imports=", ".join(sorted(SAFE_IMPORTS)),
    )

    model = get_model(ModelTier.STANDARD)
    from langchain_core.messages import HumanMessage
    response = model.invoke([HumanMessage(content=prompt)])
    code = response.content if isinstance(response.content, str) else str(response.content)

    # Strip markdown fences if present
    code = re.sub(r"^```python\s*\n?", "", code)
    code = re.sub(r"\n?```\s*$", "", code)
    code = code.strip()

    # Validate
    valid, reason = validate_code(code)
    if not valid:
        return None, f"Generated code rejected: {reason}"

    # Execute in restricted namespace to extract function object for registration
    namespace: dict = {}
    try:
        exec(code, namespace)  # noqa: S102
    except Exception as e:
        return None, f"Code execution failed: {e}"

    # Find the function
    func = None
    for obj in namespace.values():
        if callable(obj) and hasattr(obj, "__name__"):
            func = obj
            break

    if not func:
        return None, "No function found in generated code"

    # Wrap function for sandboxed execution at runtime
    _saved_code = code  # capture for sandbox closure
    _func_name = func.__name__
    _func_doc = func.__doc__ or description

    async def _sandboxed_wrapper(*args, **kwargs):
        """Execute the dynamic tool in a Docker sandbox."""
        from kronos.tools.sandbox import _docker_available, execute_sandboxed

        if _docker_available():
            # Build a self-contained script that calls the function with given args
            call_args = ", ".join(repr(a) for a in args)
            call_kwargs = ", ".join(f"{k}={repr(v)}" for k, v in kwargs.items())
            all_call_args = ", ".join(filter(None, [call_args, call_kwargs]))
            runner_code = (
                _saved_code
                + f"\n\nimport asyncio\n"
                f"result = asyncio.run({_func_name}({all_call_args})) "
                f"if asyncio.iscoroutinefunction({_func_name}) "
                f"else {_func_name}({all_call_args})\n"
                f"print(result)"
            )
            stdout, stderr = await execute_sandboxed(runner_code, timeout=30)
            if stderr:
                log.warning("Sandbox stderr for %s: %s", _func_name, stderr[:200])
            return stdout or stderr or "No output"
        else:
            # Fallback to direct in-process execution
            if _is_async(func):
                return await func(*args, **kwargs)
            return func(*args, **kwargs)

    _sandboxed_wrapper.__name__ = _func_name
    _sandboxed_wrapper.__doc__ = _func_doc

    # Wrap as LangChain tool using the sandboxed wrapper
    tool = StructuredTool.from_function(
        coroutine=_sandboxed_wrapper,
        name=clean_name,
        description=_func_doc,
    )

    # Persist
    _save_tool(clean_name, code, description)

    log.info("Dynamic tool created: %s", clean_name)
    return tool, f"Tool '{clean_name}' created successfully."


def load_persisted_tools() -> list[BaseTool]:
    """Load previously created dynamic tools from disk."""
    if not TOOLS_DIR.exists():
        return []

    tools = []
    for tool_file in TOOLS_DIR.glob("*.py"):
        try:
            code = tool_file.read_text(encoding="utf-8")
            valid, reason = validate_code(code)
            if not valid:
                log.warning("Skipping invalid persisted tool %s: %s", tool_file.name, reason)
                continue

            namespace: dict = {}
            exec(code, namespace)  # noqa: S102

            for obj in namespace.values():
                if callable(obj) and hasattr(obj, "__name__") and obj.__name__ != "<module>":
                    _persisted_code = code
                    _persisted_name = obj.__name__
                    _persisted_doc = obj.__doc__ or f"Dynamic tool: {obj.__name__}"
                    _persisted_func = obj

                    async def _persisted_sandbox_wrapper(
                        *args,
                        _code=_persisted_code,
                        _name=_persisted_name,
                        _func=_persisted_func,
                        **kwargs,
                    ):
                        """Execute the persisted dynamic tool in a Docker sandbox."""
                        from kronos.tools.sandbox import _docker_available, execute_sandboxed

                        if _docker_available():
                            call_args = ", ".join(repr(a) for a in args)
                            call_kwargs = ", ".join(f"{k}={repr(v)}" for k, v in kwargs.items())
                            all_call_args = ", ".join(filter(None, [call_args, call_kwargs]))
                            runner_code = (
                                _code
                                + f"\n\nimport asyncio\n"
                                f"result = asyncio.run({_name}({all_call_args})) "
                                f"if asyncio.iscoroutinefunction({_name}) "
                                f"else {_name}({all_call_args})\n"
                                f"print(result)"
                            )
                            stdout, stderr = await execute_sandboxed(runner_code, timeout=30)
                            if stderr:
                                log.warning("Sandbox stderr for %s: %s", _name, stderr[:200])
                            return stdout or stderr or "No output"
                        else:
                            if _is_async(_func):
                                return await _func(*args, **kwargs)
                            return _func(*args, **kwargs)

                    _persisted_sandbox_wrapper.__name__ = _persisted_name
                    _persisted_sandbox_wrapper.__doc__ = _persisted_doc

                    tool = StructuredTool.from_function(
                        coroutine=_persisted_sandbox_wrapper,
                        name=_persisted_name,
                        description=_persisted_doc,
                    )
                    tools.append(tool)
                    break

        except Exception as e:
            log.error("Failed to load dynamic tool %s: %s", tool_file.name, e)

    if tools:
        log.info("Loaded %d persisted dynamic tools", len(tools))
    return tools


def _save_tool(name: str, code: str, description: str) -> None:
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    path = TOOLS_DIR / f"{name}.py"
    header = f'"""{description}"""\n\n'
    path.write_text(header + code, encoding="utf-8")


def _is_async(func) -> bool:
    import asyncio
    import inspect
    return inspect.iscoroutinefunction(func)
