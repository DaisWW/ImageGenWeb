from __future__ import annotations

from dataclasses import dataclass

from .config.service import RuntimeConfigService
from .services import (
    AuthService,
    BillingService,
    ConversationService,
    GenerationService,
    ImageLibraryService,
    RuntimeLogService,
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
    image_library: ImageLibraryService
    generations: GenerationService
    conversations: ConversationService
    runtime_logs: RuntimeLogService
    settings: SystemSettingsService
    configuration: RuntimeConfigService
