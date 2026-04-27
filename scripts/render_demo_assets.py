#!/usr/bin/env python3
"""Render public-safe KAOS demo visuals.

The script always writes the SVG storyboard. If `rsvg-convert` and `ffmpeg`
are available, it also renders the animated GIF used by the README.
"""

from __future__ import annotations

import html
import shutil
import subprocess
import textwrap
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "docs" / "assets"
STORYBOARD = ASSETS / "kaos-durable-agent-demo.svg"
GIF = ASSETS / "kaos-durable-agent-demo.gif"
PERSONAL_STORYBOARD = ASSETS / "kaos-personal-operator-demo.svg"
PERSONAL_GIF = ASSETS / "kaos-personal-operator-demo.gif"
SWARM_STORYBOARD = ASSETS / "kaos-swarm-mode-demo.svg"
SWARM_GIF = ASSETS / "kaos-swarm-mode-demo.gif"

STEPS = [
    {
        "label": "Ask",
        "title": "Plan a launch briefing",
        "body": "The user starts one local session with safe demo data.",
        "panel": "CLI prompt",
    },
    {
        "label": "Memory",
        "title": "Recall durable preferences",
        "body": "KAOS recalls concise technical answers and public-safe launch notes.",
        "panel": "3 facts, 1 session",
    },
    {
        "label": "Skill",
        "title": "Load research-brief skill",
        "body": "Behavior is packaged as a reviewed workspace skill, not hidden in prompts.",
        "panel": "research-brief",
    },
    {
        "label": "Tools",
        "title": "Audit every tool call",
        "body": "Allowed tool calls are logged; risky MCP mutation is blocked by policy.",
        "panel": "tool audit",
    },
    {
        "label": "Jobs",
        "title": "Show scheduled work",
        "body": "Heartbeat succeeds while an unsafe demo brief remains paused.",
        "panel": "heartbeat ok",
    },
    {
        "label": "Control",
        "title": "Inspect the control room",
        "body": "Dashboard connects runtime, memory, jobs, approvals, audit, and swarm.",
        "panel": "KAOS Control Room",
    },
]

PERSONAL_STEPS = [
    {
        "label": "Inbox",
        "title": "Triage a generic inbox",
        "body": "The operator turns safe fixture notes into decisions and next actions.",
        "panel": "3 public notes",
    },
    {
        "label": "Memory",
        "title": "Use recurring preferences",
        "body": "It remembers concise updates, risk-first planning, and weekly review cadence.",
        "panel": "operator preferences",
    },
    {
        "label": "Skills",
        "title": "Apply task-breakdown",
        "body": "The productivity skill converts fuzzy work into owners, risks, and follow-ups.",
        "panel": "task-breakdown",
    },
    {
        "label": "Review",
        "title": "Ask before external action",
        "body": "The demo stops at a reviewable plan instead of sending messages or mutating tools.",
        "panel": "approval required",
    },
    {
        "label": "Routine",
        "title": "Prepare recurring brief",
        "body": "Scheduled status makes the operator feel durable without hiding automation.",
        "panel": "daily brief paused",
    },
    {
        "label": "Control",
        "title": "Inspect everything locally",
        "body": "Dashboard shows sessions, memory, jobs, tool audit, and capability gates.",
        "panel": "local control room",
    },
]

SWARM_STEPS = [
    {
        "label": "Prompt",
        "title": "Ask for a launch plan",
        "body": "One task is broad enough to benefit from independent perspectives.",
        "panel": "single prompt",
    },
    {
        "label": "Research",
        "title": "Researcher finds patterns",
        "body": "The research role looks for comparable launch moves and evidence.",
        "panel": "researcher",
    },
    {
        "label": "Critic",
        "title": "Critic finds risks",
        "body": "The critic flags setup friction, trust gaps, and missing safety copy.",
        "panel": "critic",
    },
    {
        "label": "Operator",
        "title": "Operator turns it into tasks",
        "body": "The operator converts the plan into concrete docs, assets, and checks.",
        "panel": "operator",
    },
    {
        "label": "Arbitrate",
        "title": "Claims are coordinated",
        "body": "SQLite arbitration keeps multiple agents from producing duplicate replies.",
        "panel": "claims",
    },
    {
        "label": "Synthesis",
        "title": "One final answer wins",
        "body": "The synthesizer merges useful disagreement into one response for the user.",
        "panel": "synthesizer",
    },
]


def _text(x: int, y: int, value: str, size: int = 18, weight: int = 500, color: str = "#E5E7EB") -> str:
    return (
        f'<text x="{x}" y="{y}" fill="{color}" '
        f'font-family="Helvetica, Arial, sans-serif" font-size="{size}" '
        f'font-weight="{weight}">{html.escape(value)}</text>'
    )


def _paragraph(x: int, y: int, value: str, width: int = 54) -> list[str]:
    return [
        _text(x, y + index * 26, line, 18, 500, "#CBD5E1")
        for index, line in enumerate(textwrap.wrap(value, width=width)[:2])
    ]


def render_svg(
    active: int | None = None,
    *,
    steps: list[dict] | None = None,
    subtitle: str = "durable local agent demo",
    proof_lines: list[tuple[str, str]] | None = None,
) -> str:
    steps = steps or STEPS
    proof_lines = proof_lines or [
        ("memory recall", "#A7F3D0"),
        ("workspace skill loaded", "#93C5FD"),
        ("tool result + blocked MCP call", "#FDE68A"),
        ("scheduled job status", "#C4B5FD"),
        ("dashboard overview", "#F8FAFC"),
    ]
    active = 5 if active is None else active
    cards = []
    for index, step in enumerate(steps):
        x = 70 + index * 180
        is_active = index == active
        fill = "#1E293B" if is_active else "#111827"
        stroke = "#38BDF8" if is_active else "#334155"
        cards.append(f'<rect x="{x}" y="470" width="148" height="126" rx="14" fill="{fill}" stroke="{stroke}" stroke-width="2"/>')
        cards.append(_text(x + 18, 502, step["label"], 16, 800, "#F8FAFC" if is_active else "#CBD5E1"))
        cards.append(_text(x + 18, 532, step["panel"], 13, 500, "#93C5FD" if is_active else "#94A3B8"))
        cards.append(f'<circle cx="{x + 74}" cy="576" r="10" fill="{"#22C55E" if index <= active else "#475569"}"/>')

    proof_text = []
    for index, (value, color) in enumerate(proof_lines):
        proof_text.append(_text(658, 260 + index * 34, value, 16, 700, color))

    step = steps[active]
    return "\n".join([
        '<svg width="1200" height="720" viewBox="0 0 1200 720" fill="none" xmlns="http://www.w3.org/2000/svg" role="img" aria-labelledby="title desc">',
        '  <title id="title">KAOS durable agent demo</title>',
        '  <desc id="desc">A public-safe storyboard showing a durable KAOS agent using memory, skills, tool audit, jobs, and dashboard control room.</desc>',
        '  <rect width="1200" height="720" rx="24" fill="#0B1020"/>',
        '  <rect x="36" y="36" width="1128" height="648" rx="22" fill="#111827" stroke="#253247"/>',
        '  <rect x="70" y="70" width="1060" height="78" rx="18" fill="#172033" stroke="#2B3A52"/>',
        _text(102, 116, "Kronos Agent OS", 30, 800, "#F8FAFC"),
        _text(388, 116, subtitle, 18, 600, "#94A3B8"),
        '  <rect x="914" y="91" width="174" height="34" rx="17" fill="#12352C" stroke="#2DD4BF"/>',
        _text(940, 114, "safe demo data", 14, 800, "#A7F3D0"),
        '  <rect x="70" y="178" width="500" height="244" rx="18" fill="#0F172A" stroke="#26364E"/>',
        _text(102, 222, "Step " + str(active + 1) + ": " + step["title"], 30, 800, "#F8FAFC"),
        *_paragraph(102, 262, step["body"]),
        '  <rect x="102" y="306" width="414" height="64" rx="12" fill="#172033" stroke="#334155"/>',
        _text(130, 346, step["panel"], 22, 800, "#93C5FD"),
        '  <rect x="626" y="178" width="504" height="244" rx="18" fill="#0F172A" stroke="#26364E"/>',
        _text(658, 220, "What viewers see", 22, 800, "#F8FAFC"),
        *proof_text,
        '  <path d="M70 533H1130" stroke="#334155" stroke-width="4"/>',
        *cards,
        '</svg>',
    ])


def _render_gif(steps: list[dict], output: Path, *, subtitle: str, proof_lines: list[tuple[str, str]]) -> str:
    if not shutil.which("rsvg-convert") or not shutil.which("ffmpeg"):
        return "skipped: rsvg-convert and ffmpeg are required"

    with tempfile.TemporaryDirectory(prefix="kaos-demo-frames-") as tmp:
        tmp_dir = Path(tmp)
        for index in range(len(steps)):
            svg_path = tmp_dir / f"frame-{index:02d}.svg"
            png_path = tmp_dir / f"frame-{index:02d}.png"
            svg_path.write_text(render_svg(index, steps=steps, subtitle=subtitle, proof_lines=proof_lines), encoding="utf-8")
            subprocess.run(["rsvg-convert", str(svg_path), "-o", str(png_path)], check=True)

        palette = tmp_dir / "palette.png"
        subprocess.run([
            "ffmpeg",
            "-y",
            "-framerate",
            "1",
            "-i",
            str(tmp_dir / "frame-%02d.png"),
            "-vf",
            "palettegen",
            str(palette),
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run([
            "ffmpeg",
            "-y",
            "-framerate",
            "1",
            "-i",
            str(tmp_dir / "frame-%02d.png"),
            "-i",
            str(palette),
            "-lavfi",
            "paletteuse",
            "-loop",
            "0",
            str(output),
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return str(output)


def render_assets() -> dict[str, str]:
    ASSETS.mkdir(parents=True, exist_ok=True)
    STORYBOARD.write_text(render_svg(), encoding="utf-8")
    personal_lines = [
        ("personal operator template", "#A7F3D0"),
        ("inbox + task workflow", "#93C5FD"),
        ("review before external action", "#FDE68A"),
        ("recurring brief visibility", "#C4B5FD"),
        ("local dashboard control", "#F8FAFC"),
    ]
    PERSONAL_STORYBOARD.write_text(
        render_svg(
            steps=PERSONAL_STEPS,
            subtitle="personal operator demo",
            proof_lines=personal_lines,
        ),
        encoding="utf-8",
    )
    swarm_lines = [
        ("researcher + critic + operator", "#A7F3D0"),
        ("claim arbitration", "#93C5FD"),
        ("intermediate outputs", "#FDE68A"),
        ("one synthesized answer", "#C4B5FD"),
        ("optional KAOS mode", "#F8FAFC"),
    ]
    SWARM_STORYBOARD.write_text(
        render_svg(
            steps=SWARM_STEPS,
            subtitle="swarm mode demo",
            proof_lines=swarm_lines,
        ),
        encoding="utf-8",
    )

    outputs = {
        "durable_svg": str(STORYBOARD),
        "personal_svg": str(PERSONAL_STORYBOARD),
        "swarm_svg": str(SWARM_STORYBOARD),
        "durable_gif": _render_gif(
            STEPS,
            GIF,
            subtitle="durable local agent demo",
            proof_lines=[
                ("memory recall", "#A7F3D0"),
                ("workspace skill loaded", "#93C5FD"),
                ("tool result + blocked MCP call", "#FDE68A"),
                ("scheduled job status", "#C4B5FD"),
                ("dashboard overview", "#F8FAFC"),
            ],
        ),
        "personal_gif": _render_gif(
            PERSONAL_STEPS,
            PERSONAL_GIF,
            subtitle="personal operator demo",
            proof_lines=personal_lines,
        ),
        "swarm_gif": _render_gif(
            SWARM_STEPS,
            SWARM_GIF,
            subtitle="swarm mode demo",
            proof_lines=swarm_lines,
        ),
    }
    return outputs


if __name__ == "__main__":
    for kind, path in render_assets().items():
        print(f"{kind}: {path}")
