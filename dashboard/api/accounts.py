"""Site accounts in the control room: which sites, what the agent may do there.

Read endpoints never return a profile path, a login, or anything secret-adjacent
— the same rule the agent's tools follow. What the owner needs to see is which
sites are configured, what each permits, and whether the session is still alive.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from dashboard.auth import verify_token
from kronos import accounts, vault

router = APIRouter(prefix="/api/accounts", tags=["accounts"], dependencies=[Depends(verify_token)])
log = logging.getLogger("kronos.dashboard.accounts")


class AccountPayload(BaseModel):
    site: str
    domains: list[str] = Field(default_factory=list)
    login: str = ""
    profile_dir: str = ""
    permission: str = accounts.PERMISSION_READ
    approval_required: bool = True
    notes: str = ""
    # Write-only. Sent when the owner types one, absent on every other save —
    # so editing a permission cannot silently wipe the credentials, and nothing
    # ever has to round-trip a password back through the browser to save a form.
    password: str = ""


class PasswordPayload(BaseModel):
    password: str


def _view(account: accounts.SiteAccount) -> dict:
    """What the control room shows. Note what is absent."""
    return {
        "site": account.site,
        "domains": account.domains,
        # The login identifies the account to its owner, who already knows it;
        # the profile path and the password are deliberately not exposed anywhere.
        "login": account.login,
        "has_profile": bool(account.profile_dir),
        "has_password": account.has_password,
        "permission": account.permission,
        "approval_required": account.approval_required,
        "session_state": account.session_state,
        "last_used_at": account.last_used_at,
        "notes": account.notes,
    }


@router.get("/")
async def list_site_accounts():
    return {
        "accounts": [_view(account) for account in accounts.list_accounts()],
        "permissions": list(accounts.PERMISSIONS),
        "actions": sorted(accounts.ACTION_PERMISSION),
        # So the UI can explain why storing a password is unavailable, and where
        # the key is held, instead of offering a field that quietly fails.
        "password_vault_enabled": vault.available(),
        "vault_key_source": vault.key_source(),
        "vault_hint": vault.NO_KEY_HINT,
    }


@router.post("/")
async def save_site_account(payload: AccountPayload):
    try:
        account = accounts.save_account(
            site=payload.site,
            domains=payload.domains,
            login=payload.login,
            profile_dir=payload.profile_dir,
            permission=payload.permission,
            approval_required=payload.approval_required,
            notes=payload.notes,
            password=payload.password,
        )
    except accounts.AccountError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return _view(account)


@router.put("/{site}/password")
async def set_site_password(site: str, payload: PasswordPayload):
    """Store a password for an existing account. There is no endpoint to read one."""
    try:
        accounts.set_password(site, payload.password)
    except accounts.AccountError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return _view(accounts.get_account(site))


@router.delete("/{site}/password")
async def clear_site_password(site: str):
    if not accounts.clear_password(site):
        raise HTTPException(status_code=404, detail=f"no password stored for '{site}'")
    return _view(accounts.get_account(site))


@router.delete("/{site}")
async def delete_site_account(site: str):
    if not accounts.delete_account(site):
        raise HTTPException(status_code=404, detail=f"no account for '{site}'")
    return {"deleted": site}
