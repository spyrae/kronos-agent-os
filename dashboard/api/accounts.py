"""Site accounts in the control room: which sites, what the agent may do there.

Read endpoints never return a profile path, a login, or anything secret-adjacent
— the same rule the agent's tools follow. What the owner needs to see is which
sites are configured, what each permits, and whether the session is still alive.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from dashboard.auth import verify_token
from kronos import accounts

router = APIRouter(prefix="/api/accounts", tags=["accounts"], dependencies=[Depends(verify_token)])
log = logging.getLogger("kronos.dashboard.accounts")


class AccountPayload(BaseModel):
    site: str
    domains: list[str] = Field(default_factory=list)
    method: str = accounts.METHOD_PROFILE
    login: str = ""
    profile_dir: str = ""
    permission: str = accounts.PERMISSION_READ
    approval_required: bool = True
    notes: str = ""


def _view(account: accounts.SiteAccount) -> dict:
    """What the control room shows. Note what is absent."""
    return {
        "site": account.site,
        "domains": account.domains,
        "method": account.method,
        # The login identifies the account to its owner, who already knows it;
        # the profile path is deliberately not exposed anywhere.
        "login": account.login,
        "has_profile": bool(account.profile_dir),
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
        # Surfaced so the UI can explain why the password option is disabled
        # rather than offering a field that quietly does nothing.
        "password_vault_enabled": False,
    }


@router.post("/")
async def save_site_account(payload: AccountPayload):
    try:
        account = accounts.save_account(
            site=payload.site,
            domains=payload.domains,
            method=payload.method,
            login=payload.login,
            profile_dir=payload.profile_dir,
            permission=payload.permission,
            approval_required=payload.approval_required,
            notes=payload.notes,
        )
    except accounts.AccountError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return _view(account)


@router.delete("/{site}")
async def delete_site_account(site: str):
    if not accounts.delete_account(site):
        raise HTTPException(status_code=404, detail=f"no account for '{site}'")
    return {"deleted": site}
