"""Access Control module.

Responsibility: accounts, login, lockout, sessions and the role table; decide
whether a session may perform a function and record every login and every
refusal.
Secret: how passwords are stored and checked, how sessions are represented,
and the role-to-function table. "Nurses may now acknowledge alarms" changes
PERMISSIONS and nothing else.

D1 traceability: FR-AUT-01 to FR-AUT-05, NFR-SEC-01, NFR-AUD-03 (login part),
role table in D1 section 8, hazard H1.

State invariant I-AUT:
  * every account has exactly one role (FR-AUT-04);
  * a disabled account has no session;
  * a session's last-activity time never exceeds the current time;
  * failed-attempt counter < lockout_attempts unless the account is locked.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Dict, List, Optional, Set

from .clock import Clock
from .domain import (Authorisation, Category, ChangeKind, Fn, Result, Role, FailSafe)
from .history_store import HistoryStore
from .policy import Policy

A, P, N, T = Role.ADMINISTRATOR, Role.PHARMACIST, Role.NURSE, Role.TECHNICIAN

# D1 section 8 role table (the secret of this module).
PERMISSIONS: Dict[Fn, Set[Role]] = {
    Fn.VIEW_DASHBOARD:          {P, N, T, A},
    Fn.ACK_ALARM:               {P, T},
    Fn.CHANGE_THRESHOLD:        {P},
    Fn.VIEW_HISTORY:            {P, T, A},
    Fn.RUN_INVENTORY_REPORT:    {P},
    Fn.RUN_ALARM_AVAIL_REPORTS: {P, T},
    Fn.DECIDE_MAINTENANCE:      {T},
    Fn.ADMIN:                   {A},
}


@dataclass
class _User:
    user_id: str
    role: Role
    salt: str
    pw_hash: str
    disabled: bool = False
    failed: int = 0
    locked_until: float = 0.0


@dataclass
class _Session:
    token: str
    user_id: str
    last_activity: float


@dataclass(frozen=True)
class SessionInfo:
    """Identity and role of a valid session, without its token."""
    user_id: str
    role: Role


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 60_000).hex()


class AccessControl:
    """Manage accounts, sessions, lockout and role permissions."""
    def __init__(self, history: HistoryStore, clock: Clock, policy: Policy, mode):
        """Initialise the module with its injected collaborators and empty state."""
        self._h = history
        self._clock = clock
        self._p = policy
        self._mode = mode          # System Mode Manager (answers allows(ChangeKind))
        self._users: Dict[str, _User] = {}
        self._sessions: Dict[str, _Session] = {}

    # ----------------------------------------------------------- accounts
    def has_users(self) -> bool:
        """Return whether any account has been created."""
        return bool(self._users)

    def bootstrap_administrator(self, user_id: str, password: str) -> Result:
        """Create the first administrator when no account exists (assumption A10:
        accounts exist before the system is used). Refused once any account exists."""
        if self._users:
            return Result.refused("accounts already exist")
        return self._create(user_id, Role.ADMINISTRATOR, password, actor="system")

    def create_user(self, auth: Authorisation, user_id: str, role: Role, password: str) -> Result:
        """FR-AUT-04. Pre: auth.fn is ADMIN. Post: account exists with exactly
        one role and the audit record holds user ID, time, old (none) and new."""
        if auth.fn is not Fn.ADMIN:
            return self._refuse(auth.user_id, "not permitted")
        if not isinstance(role, Role):
            return self._refuse(auth.user_id, "an account needs exactly one role")
        gate = self._mode.allows(ChangeKind.ACCOUNT)
        if not gate:
            return self._refuse(auth.user_id, gate.reason)
        return self._create(user_id, role, password, actor=auth.user_id)

    def disable_user(self, auth: Authorisation, user_id: str) -> Result:
        """FR-AUT-05. Post: the account cannot log in and its sessions end."""
        if auth.fn is not Fn.ADMIN:
            return self._refuse(auth.user_id, "not permitted")
        gate = self._mode.allows(ChangeKind.ACCOUNT)
        if not gate:
            return self._refuse(auth.user_id, gate.reason)
        user = self._users.get(user_id)
        if user is None:
            return self._refuse(auth.user_id, "unknown user")
        if user.disabled:
            return self._refuse(auth.user_id, "already disabled")
        user.disabled = True
        self._drop_sessions_of(user_id)
        self._h.append(Category.USER_ACTION, "user_disabled",
                       {"target": user_id, "old": "active", "new": "disabled"}, user_id=auth.user_id)
        self._check_invariant()
        return Result.success()

    def list_users(self) -> List[dict]:
        """List account identities, roles and disabled flags without credentials."""
        return [{"user_id": u.user_id, "role": u.role, "disabled": u.disabled}
                for u in sorted(self._users.values(), key=lambda x: x.user_id)]

    # ------------------------------------------------------------- login
    def login(self, user_id: str, password: str) -> Result:
        """FR-AUT-01, FR-AUT-03. Function table (rows evaluated in order):
          L1 account locked                      -> refused, attempt recorded, counter unchanged
          L2 unknown or disabled account         -> refused, attempt recorded
          L3 wrong password, failed+1 <  limit   -> refused, counter + 1
          L4 wrong password, failed+1 >= limit   -> refused, locked for lockout_duration_s
          L5 right password                      -> session token, counter reset
        The reason shown to the caller never says which of L2 and L3 applied."""
        now = self._clock.now()
        user = self._users.get(user_id)
        if user is not None and user.locked_until > now:
            self._h.append(Category.ACCESS, "login_failed", {"reason": "account locked"}, user_id=user_id)
            return Result.refused("account locked")
        if user is None or user.disabled:
            self._h.append(Category.ACCESS, "login_failed",
                           {"reason": "unknown or disabled account"}, user_id=user_id)
            return Result.refused("wrong user ID or password")
        if not hmac.compare_digest(_hash(password, user.salt), user.pw_hash):
            self._register_failure(user, now)
            self._h.append(Category.ACCESS, "login_failed", {"reason": "wrong password",
                                                            "failed_in_a_row": user.failed},
                           user_id=user_id)
            if user.locked_until > now:
                self._h.append(Category.ACCESS, "locked", {"until": user.locked_until}, user_id=user_id)
            self._check_invariant()
            return Result.refused("wrong user ID or password")
        user.failed = 0
        token = secrets.token_urlsafe(24)
        self._sessions[token] = _Session(token, user_id, now)
        self._h.append(Category.ACCESS, "login_ok", {}, user_id=user_id)
        self._check_invariant()
        return Result.success(token)

    def logout(self, token: str) -> None:
        """End the named session and record its logout."""
        s = self._sessions.pop(token, None)
        if s:
            self._h.append(Category.ACCESS, "logout", {}, user_id=s.user_id)

    # ---------------------------------------------------------- sessions
    def session(self, token: Optional[str], touch: bool = True) -> Optional[SessionInfo]:
        """NFR-SEC-01. Returns None when the token is unknown, the account is
        disabled, or the session was idle for session_idle_s (the session is
        then ended and the expiry recorded)."""
        if not token:
            return None
        s = self._sessions.get(token)
        if s is None:
            return None
        now = self._clock.now()
        user = self._users.get(s.user_id)
        if user is None or user.disabled:
            self._sessions.pop(token, None)
            return None
        if now - s.last_activity >= self._p.session_idle_s:
            self._sessions.pop(token, None)
            self._h.append(Category.ACCESS, "session_expired", {}, user_id=s.user_id)
            return None
        if touch:
            s.last_activity = now
        return SessionInfo(user.user_id, user.role)

    def authorise(self, token: Optional[str], fn: Fn, *, touch: bool = True) -> Optional[Authorisation]:
        """FR-AUT-01, FR-AUT-02, NFR-AUD-03.
        Function table:
          A1 no valid session                         -> None (no cabinet data may be shown)
          A2 valid session, role in PERMISSIONS[fn]   -> Authorisation(user, role, fn)
          A3 valid session, role not in PERMISSIONS   -> None, refusal recorded with user and time"""
        if fn not in PERMISSIONS:
            raise FailSafe("TBL-AUT-FUNCTION", str(fn))
        info = self.session(token, touch=touch)
        if info is None:
            return None
        if info.role in PERMISSIONS[fn]:
            return Authorisation(info.user_id, info.role, fn)
        self._h.append(Category.ACCESS, "access_refused",
                       {"function": fn.value, "role": info.role.value}, user_id=info.user_id)
        return None

    def permits(self, role: Role, fn: Fn) -> bool:
        """Return whether the role may perform the named function."""
        return role in PERMISSIONS[fn]

    def active_sessions(self) -> int:
        """Count sessions currently held in memory."""
        return len(self._sessions)

    # -------------------------------------------------------------- restore
    def restore(self, events) -> None:
        """Rebuild accounts and lockout counters from the history."""
        for rec, secret in events:
            if rec.category is Category.USER_ACTION and rec.kind == "user_created":
                d = rec.data
                self._users[d["target"]] = _User(d["target"], Role(d["role"]),
                                                 secret["salt"], secret["pw_hash"])
            elif rec.category is Category.USER_ACTION and rec.kind == "user_disabled":
                u = self._users.get(rec.data["target"])
                if u:
                    u.disabled = True
            elif rec.category is Category.USER_ACTION and rec.kind == "user_password_changed":
                u = self._users.get(rec.data["target"])
                if u:
                    u.salt, u.pw_hash = secret["salt"], secret["pw_hash"]
            elif rec.category is Category.ACCESS and rec.user_id in self._users:
                u = self._users[rec.user_id]
                if rec.kind == "login_ok":
                    u.failed = 0
                elif rec.kind == "login_failed" and rec.data.get("reason") == "wrong password":
                    self._register_failure(u, rec.ts)
        self._check_invariant()

    # ------------------------------------------------------------ internals
    def _roles(self):
        """Supply account form choices without exposing the role representation."""
        return list(Role)

    def _role_for_name(self, name):
        """Resolve an account form choice, returning None for an unknown name."""
        return Role.__members__.get(name)

    def _seed_demo_account(self, auth: Authorisation, user_id: str, role: Role,
                           password: str) -> Result:
        """Trusted demo bootstrap only; normal account creation keeps its length rule.

        Existing accounts retain their role, disabled state and lockout counters.
        Return whether credentials changed so repeated seeding stays idempotent.
        """
        if auth.fn is not Fn.ADMIN:
            return self._refuse(auth.user_id, "not permitted")
        gate = self._mode.allows(ChangeKind.ACCOUNT)
        if not gate:
            return self._refuse(auth.user_id, gate.reason)
        user = self._users.get(user_id)
        if user is None:
            result = self._create(user_id, role, password, actor=auth.user_id,
                                  minimum_password_length=1)
            return Result.success(True) if result.ok else result
        if user.role is not role:
            return self._refuse(auth.user_id, "demo account has a different role")
        if hmac.compare_digest(_hash(password, user.salt), user.pw_hash):
            return Result.success(False)
        user.salt = secrets.token_hex(8)
        user.pw_hash = _hash(password, user.salt)
        self._drop_sessions_of(user_id)
        self._h.append(Category.USER_ACTION, "user_password_changed",
                       {"target": user_id, "old": "previous password", "new": "demo password"},
                       user_id=auth.user_id, secret={"salt": user.salt, "pw_hash": user.pw_hash})
        self._check_invariant()
        return Result.success(True)

    def _create(self, user_id: str, role: Role, password: str, actor: str,
                *, minimum_password_length: int = 8) -> Result:
        if not user_id or not user_id.replace("-", "").replace("_", "").isalnum() or len(user_id) > 32:
            return self._refuse(actor, "user ID must be 1 to 32 letters, digits, - or _")
        if user_id in self._users:
            return self._refuse(actor, "user ID already exists")
        if len(password) < minimum_password_length:
            return self._refuse(actor, "password must have at least 8 characters")
        salt = secrets.token_hex(8)
        self._users[user_id] = _User(user_id, role, salt, _hash(password, salt))
        self._h.append(Category.USER_ACTION, "user_created",
                       {"target": user_id, "role": role.value, "old": None, "new": role.value},
                       user_id=actor, secret={"salt": salt, "pw_hash": self._users[user_id].pw_hash})
        self._check_invariant()
        return Result.success()

    def _register_failure(self, user: _User, ts: float) -> None:
        user.failed += 1
        if user.failed >= self._p.lockout_attempts:
            user.locked_until = ts + self._p.lockout_duration_s
            user.failed = 0

    def _drop_sessions_of(self, user_id: str) -> None:
        for tok in [t for t, s in self._sessions.items() if s.user_id == user_id]:
            self._sessions.pop(tok, None)

    def _check_invariant(self) -> None:
        now = self._clock.now()
        for u in self._users.values():
            if not isinstance(u.role, Role):
                raise FailSafe("INV-AUT-1", u.user_id)
            if u.failed >= self._p.lockout_attempts:
                raise FailSafe("INV-AUT-4", u.user_id)
        for s in self._sessions.values():
            u = self._users.get(s.user_id)
            if u is None or u.disabled:
                raise FailSafe("INV-AUT-2", s.user_id)
            if s.last_activity > now + 1e-6:
                raise FailSafe("INV-AUT-3", s.user_id)

    def _refuse(self, user_id: str, reason: str) -> Result:
        """Record a refused action without changing its result or application state."""
        self._h.append(Category.ACCESS, "action_refused",
                       {"module": "access_control", "reason": reason}, user_id=user_id)
        return Result.refused(reason)
