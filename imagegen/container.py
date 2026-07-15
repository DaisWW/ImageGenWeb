from __future__ import annotations

from dataclasses import dataclass

from .config.service import RuntimeConfigService
from .services import (
    AuthService,
    AutomaticTitleService,
    BillingService,
    ConversationService,
    GenerationService,
    RuntimeLogService,
    SystemSettingsService,
    UserService,
    WorkspaceService,
)


@dataclass(frozen=True, slots=True)
class ApplicationServices:
    automatic_titles: AutomaticTitleService
    auth: AuthService
    billing: BillingService
    users: UserService
    workspaces: WorkspaceService
    generations: GenerationService
    conversations: ConversationService
    runtime_logs: RuntimeLogService
    settings: SystemSettingsService
    configuration: RuntimeConfigService

    def close(self) -> None:
        self.automatic_titles.close()
