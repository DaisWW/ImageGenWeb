from __future__ import annotations

from ..errors import ServiceError
from ..extensions import db
from ..models import AuditLog, SystemState


class SystemSettingsService:
    SITE_TITLE_KEY = "site_title"
    DEFAULT_SITE_TITLE = "西郊比克王 AI Studio"

    def site_title(self) -> str:
        state = db.session.get(SystemState, self.SITE_TITLE_KEY)
        return state.value if state and state.value.strip() else self.DEFAULT_SITE_TITLE

    def set_site_title(self, title: str, actor_user_id: int) -> str:
        title = title.strip()
        if not 2 <= len(title) <= 60 or "\n" in title:
            raise ServiceError("系统 Title 长度必须在 2 到 60 个字符之间")
        state = db.session.get(SystemState, self.SITE_TITLE_KEY)
        old_title = self.site_title()
        if state is None:
            state = SystemState(key=self.SITE_TITLE_KEY, value=title)
            db.session.add(state)
        else:
            state.value = title
        db.session.add(
            AuditLog(
                actor_user_id=actor_user_id,
                action="system.title.update",
                target_type="system",
                target_id=self.SITE_TITLE_KEY,
                details={"old_title": old_title, "new_title": title},
            )
        )
        db.session.commit()
        return title
