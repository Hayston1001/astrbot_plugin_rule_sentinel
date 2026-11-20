"""QQ official mute HTTP adapter."""

import asyncio
import json
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote


class QQMutePlatform:
    TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
    API_ROOT = "https://api.sgroup.qq.com"

    def __init__(self, session, resolve_credentials, *, clock=time.monotonic, timeout=5):
        self.session, self.resolve_credentials, self.clock, self.timeout = session, resolve_credentials, clock, timeout
        self.tokens = {}
        self.lock = asyncio.Lock()

    def _contains_secret(self, value):
        if not isinstance(value, str):
            return False
        return any(secret and secret in value for credentials, token, _ in self.tokens.values()
                   for secret in (credentials[1], token))

    async def _response(self, method, url, **kwargs):
        async with asyncio.timeout(self.timeout):
            async with self.session.request(method, url, allow_redirects=False, **kwargs) as response:
                raw = await response.content.read(65537)
                if len(raw) > 65536:
                    raise ValueError("response too large")
                try:
                    data = json.loads(raw) if raw else {}
                except (ValueError, UnicodeDecodeError):
                    data = {}
                trace = response.headers.get("X-Tps-Trace-Id") or response.headers.get("X-Trace-Id")
                if not isinstance(trace, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", trace):
                    trace = None
                if self._contains_secret(trace):
                    trace = None
                return response.status, data if isinstance(data, dict) else {}, trace

    async def _token(self, platform_id):
        async with self.lock:
            credentials = self.resolve_credentials(platform_id)
            if not isinstance(credentials, tuple) or len(credentials) != 2 or any(not isinstance(value, str) or not value.strip() for value in credentials):
                raise ValueError("platform credentials unavailable")
            app_id, secret = credentials
            cached = self.tokens.get(platform_id)
            if cached and cached[0] == credentials and cached[2] > self.clock():
                return cached[1]
            status, data, _ = await self._response("POST", self.TOKEN_URL, json={"appId": app_id, "clientSecret": secret})
            token, expires = data.get("access_token"), data.get("expires_in")
            if status != 200 or not isinstance(token, str) or not token.strip() or isinstance(expires, bool):
                raise ValueError("token unavailable")
            try:
                expires = int(expires)
            except (TypeError, ValueError, OverflowError):
                raise ValueError("invalid token expiry") from None
            if not 1 <= expires <= 86400:
                raise ValueError("invalid token expiry")
            self.tokens[platform_id] = (credentials, token, self.clock() + max(0, expires - 60))
            return token

    async def _verify_group_role(self, platform_id, group_id, *, admin_required):
        try:
            token = await self._token(platform_id)
            status, data, _ = await self._response("GET", f"{self.API_ROOT}/v2/groups/{quote(group_id, safe='')}/bot_state",
                                                   headers={"Authorization": f"QQBot {token}"})
            if status == 401:
                self.tokens.pop(platform_id, None)
            if status in (401, 403):
                return False
            if status == 200 and data.get("member_role") in ("admin", "member", "owner"):
                return data["member_role"] == "admin" if admin_required else True
            return None
        except Exception:
            return None

    async def verify_permission(self, platform_id, group_id):
        return await self._verify_group_role(platform_id, group_id, admin_required=True)

    async def verify_reply_permission(self, platform_id, group_id):
        return await self._verify_group_role(platform_id, group_id, admin_required=False)

    async def prepare_mute(self, platform_id, group_id, sender_id, expires_at, duration_seconds, *, retry=False):
        """Resolve the QQ operation without changing the member's mute."""
        try:
            token = await self._token(platform_id)
            status, data, trace = await self._response(
                "GET", f"{self.API_ROOT}/v2/groups/{quote(group_id, safe='')}/restrict_chat_setting",
                headers={"Authorization": f"QQBot {token}"},
            )
            if status == 401:
                self.tokens.pop(platform_id, None)
            members = data.get("members")
            if status != 200 or not isinstance(members, list) or any(
                data.get(key) for key in ("has_more", "next_cursor", "next_page_token", "truncated")
            ):
                return {"outcome": "review", "reason_code": "mute_status_unavailable", "trace_id": trace}
            if any(not isinstance(member, dict) or not isinstance(member.get("member_openid"), str)
                   or not member["member_openid"].strip() for member in members):
                return {"outcome": "review", "reason_code": "mute_status_unavailable", "trace_id": trace}
            now = datetime.now(timezone.utc)
            if datetime.fromisoformat(expires_at) <= now:
                return {"outcome": "blocked", "reason_code": "intent_expired"}
            matches = [member for member in members if member["member_openid"] == sender_id]
            if not matches:
                return {"operation": "add", "expires_at": expires_at}
            if len(matches) != 1:
                return {"outcome": "review", "reason_code": "mute_status_unavailable", "trace_id": trace}
            previous = datetime.fromisoformat(matches[0]["mute_expire_at"])
            if previous.utcoffset() is None:
                raise ValueError("invalid member expiry")
            if previous <= now or retry:
                # Do not turn a failed add into an update or overwrite a
                # member whose reported mute expired during the query.
                return {"outcome": "review", "reason_code": "mute_status_changed", "trace_id": trace}
            # The configured maximum limits this action's duration. QQ's
            # 30-day maximum also bounds the final remaining mute.
            deadline = min(previous + timedelta(seconds=duration_seconds), now + timedelta(days=30))
            deadline = deadline.astimezone(timezone.utc)
            new_expiry = deadline.isoformat(timespec="milliseconds")
            if datetime.fromisoformat(new_expiry) <= previous:
                return {"outcome": "blocked", "reason_code": "mute_limit_reached", "trace_id": trace}
            return {"operation": "update", "expires_at": new_expiry}
        except Exception:
            return {"outcome": "review", "reason_code": "mute_status_unavailable"}

    async def mute(self, platform_id, group_id, sender_id, expires_at, *, operation="add"):
        if operation not in ("add", "update"):
            raise ValueError("invalid QQ mute operation")
        try:
            token = await self._token(platform_id)
            status, data, trace = await self._response("POST", f"{self.API_ROOT}/v2/groups/{quote(group_id, safe='')}/restrict_chat_setting",
                headers={"Authorization": f"QQBot {token}"},
                json={"members": [{"op": operation, "member_openid": sender_id, "mute_expire_at": expires_at}]})
        except Exception:
            return {"outcome": "unknown"}
        code = data.get("code")
        code = str(code) if type(code) is int or (isinstance(code, str) and re.fullmatch(r"[0-9-]{1,20}", code)) else str(status)
        reason = None
        if 200 <= status < 300:
            outcome = "succeeded"
        elif status >= 500 or status == 408 or status < 400:
            outcome = "unknown"
        elif code == "40103004":
            outcome, reason = "blocked", "protected_target"
        elif status in (401, 403) or (code == "11703" and str(data.get("err_code")) == "40011030"):
            outcome, reason = "permission_denied", "permission_denied"
        else:
            outcome = "failed"
        if self._contains_secret(code):
            code = None
        if status == 401:
            self.tokens.pop(platform_id, None)
        result = {"outcome": outcome, "platform_code": code, "trace_id": trace}
        if reason:
            result["reason_code"] = reason
        return result

    async def send_group_message(self, platform_id, group_id, content, *, message_id=None, msg_seq=None,
                                 reference_id=None, markdown=True, mention_id=None):
        if (markdown and isinstance(mention_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", mention_id)
                and mention_id.lower() != "all"):
            mention = f"<@{mention_id}>"
            if mention not in content:
                content = f"{mention} {content}"
        body = {"msg_type": 2, "markdown": {"content": content}} if markdown else {"content": content, "msg_type": 0}
        if reference_id is not None:
            body["message_reference"] = {"message_id": reference_id}
        if message_id is not None:
            body["msg_id"] = message_id
            body["msg_seq"] = msg_seq
        try:
            token = await self._token(platform_id)
            status, data, trace = await self._response("POST", f"{self.API_ROOT}/v2/groups/{quote(group_id, safe='')}/messages",
                headers={"Authorization": f"QQBot {token}"}, json=body)
        except Exception:
            return {"outcome": "unknown"}
        outcome = "succeeded" if 200 <= status < 300 else "failed" if 400 <= status < 500 and status != 408 else "unknown"
        code = data.get("code")
        code = str(code) if type(code) is int or (isinstance(code, str) and re.fullmatch(r"[0-9-]{1,20}", code)) else str(status)
        if self._contains_secret(code):
            code = None
        if status == 401:
            self.tokens.pop(platform_id, None)
        result = {"outcome": outcome, "platform_code": code, "trace_id": trace}
        if status == 400 and code == "40034005":
            result["reason_code"] = "quote_expired"
        return result

    async def recall(self, platform_id, group_id, message_id):
        try:
            token = await self._token(platform_id)
            status, data, trace = await self._response("DELETE", f"{self.API_ROOT}/v2/groups/{quote(group_id, safe='')}/messages/{quote(message_id, safe='')}",
                headers={"Authorization": f"QQBot {token}"})
        except Exception:
            return {"outcome": "unknown"}
        raw = data.get("code")
        code = str(raw) if type(raw) is int or (isinstance(raw, str) and re.fullmatch(r"[0-9-]{1,20}", raw)) else str(status)
        reason = None
        if 200 <= status < 300:
            outcome = "succeeded"
        elif status >= 500 or status == 408 or status < 400:
            outcome = "unknown"
        elif code == "40062003":
            outcome, reason = "blocked", "protected_target"
        elif code == "40064004":
            outcome, reason = "blocked", "recall_expired"
        elif code == "40061001":
            outcome, reason = "failed", "invalid_message"
        elif status in (401, 403):
            outcome, reason = "permission_denied", "permission_denied"
        else:
            outcome = "failed"
        if self._contains_secret(code):
            code = None
        if status == 401:
            self.tokens.pop(platform_id, None)
        result = {"outcome": outcome, "platform_code": code, "trace_id": trace}
        if reason:
            result["reason_code"] = reason
        return result


def resolve_qq_credentials(context, platform_id):
    """Use only the selected QQ official platform instance, never config files."""
    from astrbot.core.platform.sources.qqofficial.qqofficial_platform_adapter import QQOfficialPlatformAdapter

    matches = [instance for instance in context.platform_manager.get_insts()
               if isinstance(instance, QQOfficialPlatformAdapter) and instance.meta().id == platform_id]
    if len(matches) != 1:
        raise ValueError("QQ platform unavailable")
    return matches[0].appid, matches[0].secret
