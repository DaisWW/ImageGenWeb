from __future__ import annotations

from dataclasses import dataclass

from .config.service import RuntimeConfigService
from .services import (
    AuthService,
    BillingService,
    ConversationService,
    GenerationService,
    SystemSettingsService,
    UserService,
    WorkspaceService,
)


@dataclass(frozen=True, slots=True)
class ApplicationServices:
    auth: AuthService
    billing: BillingService
    users: UserService
    workspaces: WorkspaceService
    generations: GenerationService
    conversations: ConversationService
    settings: SystemSettingsService
    configuration: RuntimeConfigService
