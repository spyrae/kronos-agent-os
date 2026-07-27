"""Provenance for skills: checksum, signature, version compatibility.

A skill is a procedure the agent will follow. Importing one is not running code,
but it is accepting instructions — so "where did this come from and has it
changed since" has to be answerable without trusting the transport.

Three independent questions, deliberately kept apart:

* **checksum** — is this the same content the publisher hashed? Cheap, offline,
  catches tampering in transit and accidental edits. Useless against an attacker
  who controls the file, since they can rewrite the checksum too.
* **signature** — did a key we trust vouch for this checksum? That is the part a
  file-level attacker cannot forge. Verified through `ssh-keygen -Y verify`, so
  no new dependency and the same keys people already use for signed commits.
* **compatibility** — does this skill claim to need a KAOS this old/new?

What the checksum covers is an explicit allowlist of fields, not "the file". The
import path rewrites frontmatter on the way in (status becomes draft, provenance
fields are added), so hashing the raw file would make every imported skill fail
its own checksum. Semantic fields are hashed; local bookkeeping is not.
"""

import hashlib
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from kronos.skills.store import Skill, _parse_frontmatter, _parse_list_field

log = logging.getLogger("kronos.skills.integrity")

HASH_PREFIX = "sha256:"

# Signing namespace for `ssh-keygen -Y sign -n kaos-skill`.
SIGNATURE_NAMESPACE = "kaos-skill"

# Fields the checksum covers. An allowlist rather than an exclusion list: a new
# local bookkeeping field must not silently invalidate published checksums, and a
# new *semantic* field has to be added here on purpose (and documented).
COVERED_FIELDS = ("name", "description", "version", "requires_kaos", "author", "tools", "tier")

# Fields parsed as lists, canonicalised so "[a, b]" and "[a,b]" hash the same.
LIST_FIELDS = {"tools"}


def canonical_form(meta: dict[str, str], body: str, references: dict[str, Path]) -> bytes:
    """The exact bytes a checksum is taken over.

    Stable across import (which rewrites frontmatter), across reference ordering,
    and across trailing-whitespace churn.
    """
    lines: list[str] = []
    for field in COVERED_FIELDS:
        raw = str(meta.get(field, "") or "").strip()
        if not raw:
            continue
        value = ",".join(_parse_list_field(raw)) if field in LIST_FIELDS else raw
        lines.append(f"{field}: {value}")

    parts = ["\n".join(lines), "---", body.strip()]
    for ref_name in sorted(references):
        ref_path = references[ref_name]
        text = ref_path.read_text(encoding="utf-8").strip() if ref_path.is_file() else ""
        parts.append(f"references/{ref_name}")
        parts.append(text)

    return "\n".join(parts).encode("utf-8")


def compute_checksum(skill_dir: Path) -> str:
    """Checksum of a skill directory on disk (a publisher's or a local one)."""
    skill_file = skill_dir / "SKILL.md"
    if not skill_file.is_file():
        raise FileNotFoundError(f"no SKILL.md in {skill_dir}")

    meta, body = _parse_frontmatter(skill_file.read_text(encoding="utf-8"))
    references: dict[str, Path] = {}
    refs_dir = skill_dir / "references"
    if refs_dir.is_dir():
        references = {path.stem: path for path in sorted(refs_dir.iterdir()) if path.is_file() and path.suffix == ".md"}

    return HASH_PREFIX + hashlib.sha256(canonical_form(meta, body, references)).hexdigest()


def skill_checksum(skill: Skill) -> str:
    """Checksum of an already-loaded skill, from its own files."""
    return compute_checksum(skill.path.parent)


def verify_checksum(skill: Skill) -> tuple[bool, str]:
    """Compare the declared checksum with the computed one.

    A skill with no declared checksum is not a failure — most local skills have
    none. It is reported as unverified, which is a different thing from broken.
    """
    declared = (skill.checksum or "").strip()
    if not declared:
        return False, "no checksum declared"

    try:
        actual = skill_checksum(skill)
    except OSError as e:
        return False, f"cannot read skill files: {e}"

    if not declared.startswith(HASH_PREFIX):
        declared = HASH_PREFIX + declared
    if declared != actual:
        return False, f"checksum mismatch: declared {declared[:19]}…, computed {actual[:19]}…"
    return True, "checksum matches"


def verify_signature(skill: Skill, *, trusted_keys: list[str]) -> tuple[bool, str]:
    """Verify a detached SSH signature over the skill's checksum.

    Uses `ssh-keygen -Y verify`, the same mechanism as signed git commits, so
    publishing needs no bespoke tooling and KAOS needs no crypto dependency. The
    signed payload is the checksum string, which is short and stable.

    A publisher signs with:
        ssh-keygen -Y sign -f ~/.ssh/id_ed25519 -n kaos-skill checksum.txt
    """
    signature = (skill.signature or "").strip()
    if not signature:
        return False, "no signature declared"
    if not trusted_keys:
        return False, "no trusted keys configured (registry.trusted_keys in policy.yaml)"
    if shutil.which("ssh-keygen") is None:
        return False, "ssh-keygen not available, cannot verify signatures"

    checksum_ok, checksum_detail = verify_checksum(skill)
    if not checksum_ok:
        # Signing covers the checksum, so a wrong checksum makes the signature
        # meaningless even when it verifies against a trusted key.
        return False, f"signature not checked: {checksum_detail}"

    payload = skill_checksum(skill).encode("utf-8")
    with tempfile.TemporaryDirectory(prefix="kaos-sig-") as tmp:
        tmp_dir = Path(tmp)
        allowed = tmp_dir / "allowed_signers"
        allowed.write_text("\n".join(_allowed_signer_line(key) for key in trusted_keys) + "\n", encoding="utf-8")
        sig_file = tmp_dir / "skill.sig"
        sig_file.write_text(_pem_signature(signature), encoding="utf-8")

        for identity in _identities(trusted_keys):
            result = subprocess.run(  # noqa: S603 - fixed argv, no shell
                [
                    "ssh-keygen",
                    "-Y",
                    "verify",
                    "-f",
                    str(allowed),
                    "-I",
                    identity,
                    "-n",
                    SIGNATURE_NAMESPACE,
                    "-s",
                    str(sig_file),
                ],
                input=payload,
                capture_output=True,
                timeout=15,
                check=False,
            )
            if result.returncode == 0:
                return True, f"signed by {identity}"

    return False, "signature does not match any trusted key"


def _allowed_signer_line(key: str) -> str:
    """One `allowed_signers` entry: `<identity> <keytype> <base64>`."""
    parts = key.strip().split()
    if len(parts) >= 3 and not parts[0].startswith(("ssh-", "sk-", "ecdsa-")):
        return key.strip()  # already `identity keytype base64`
    identity = parts[-1] if len(parts) >= 3 else "kaos-publisher"
    return f"{identity} {' '.join(parts[:2])}"


def _identities(trusted_keys: list[str]) -> list[str]:
    return [_allowed_signer_line(key).split()[0] for key in trusted_keys if key.strip()]


def _pem_signature(signature: str) -> str:
    """Accept both a full PEM blob and a bare base64 body."""
    if "BEGIN SSH SIGNATURE" in signature:
        return signature if signature.endswith("\n") else signature + "\n"
    body = "\n".join(signature[i : i + 70] for i in range(0, len(signature), 70))
    return f"-----BEGIN SSH SIGNATURE-----\n{body}\n-----END SSH SIGNATURE-----\n"


def check_compatibility(skill: Skill, kaos_version: str = "") -> tuple[bool, str]:
    """Evaluate `requires_kaos` against the running version.

    Supports the comma-separated comparator form (`">=0.2,<0.4"`). An unparsable
    constraint is reported as a failure rather than ignored: a skill that declares
    a requirement nobody can read should not be trusted to be compatible.
    """
    # The frontmatter parser keeps YAML quotes, and `requires_kaos: ">=0.2,<0.4"`
    # is normally written quoted — without stripping them the constraint would be
    # unparsable and every quoted requirement would fail closed.
    requirement = (skill.requires_kaos or "").strip().strip("'\"").strip()
    if not requirement:
        return True, "no version requirement"

    if not kaos_version:
        from kronos import __version__

        kaos_version = __version__

    # A source checkout without install metadata reports "0.0.0+unknown". Failing
    # closed there would mark every skill with a requirement as incompatible, and
    # the unknown is on our side, not the skill's — so say so and allow it.
    if kaos_version.startswith("0.0.0") or "unknown" in kaos_version:
        return (
            True,
            f"cannot determine the running KAOS version ({kaos_version}); requirement '{requirement}' unchecked",
        )

    current = _parse_version(kaos_version)
    for clause in [part.strip() for part in requirement.split(",") if part.strip()]:
        match = re.match(r"^(>=|<=|==|!=|>|<)?\s*(\d+(?:\.\d+)*)", clause)
        if not match:
            return False, f"cannot parse version requirement '{clause}'"
        operator, wanted_text = match.group(1) or "==", match.group(2)
        wanted = _parse_version(wanted_text)
        if not _compare(current, operator, wanted):
            return False, f"needs KAOS {requirement}, running {kaos_version}"
    return True, f"compatible with KAOS {kaos_version}"


def _parse_version(text: str) -> tuple[int, ...]:
    """Numeric prefix of a version, so "0.3.0rc1" compares as (0, 3, 0)."""
    parts: list[int] = []
    for chunk in text.strip().lstrip("vV").split("."):
        digits = re.match(r"^\d+", chunk)
        if not digits:
            break
        parts.append(int(digits.group()))
    return tuple(parts) or (0,)


def _pad(left: tuple[int, ...], right: tuple[int, ...]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    width = max(len(left), len(right))
    return left + (0,) * (width - len(left)), right + (0,) * (width - len(right))


def _compare(current: tuple[int, ...], operator: str, wanted: tuple[int, ...]) -> bool:
    left, right = _pad(current, wanted)
    return {
        ">=": left >= right,
        "<=": left <= right,
        ">": left > right,
        "<": left < right,
        "==": left == right,
        "!=": left != right,
    }[operator]


def trusted_keys() -> list[str]:
    """Publisher keys from the policy (empty means "verify nothing")."""
    try:
        from kronos.policy import get_policy

        return list(get_policy().registry.trusted_keys)
    except Exception as e:  # pragma: no cover - policy is optional
        log.debug("Could not read registry.trusted_keys: %s", e)
        return []


def verify_skill(skill: Skill, *, keys: list[str] | None = None) -> dict:
    """Everything known about one skill's provenance, in one structure."""
    keys = trusted_keys() if keys is None else keys

    checksum_ok, checksum_detail = verify_checksum(skill)
    compatible, compatibility_detail = check_compatibility(skill)
    signature_ok, signature_detail = (False, "no signature declared")
    if skill.signature:
        signature_ok, signature_detail = verify_signature(skill, trusted_keys=keys)

    return {
        "skill": skill.name,
        "version": skill.version,
        "checksum_ok": checksum_ok,
        "checksum_detail": checksum_detail,
        "signature_ok": signature_ok,
        "signature_detail": signature_detail,
        "compatible": compatible,
        "compatibility_detail": compatibility_detail,
        # "Trusted" is the conjunction a caller should gate on: a declared
        # checksum that matches, a compatible version, and — when a signature is
        # present — a signature that verifies.
        "trusted": checksum_ok and compatible and (signature_ok or not skill.signature),
        "unverified": not skill.checksum,
    }
