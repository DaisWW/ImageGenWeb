from __future__ import annotations

from dataclasses import dataclass

from .config.service import RuntimeConfigService
from .services import (
    AuthService,
    BackgroundRemovalService,
    BillingService,
    ConversationService,
    GenerationApiKeyService,
    GenerationApiService,
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
    background_removal: BackgroundRemovalService
    billing: BillingService
    users: UserService
    workspaces: WorkspaceService
    image_library: ImageLibraryService
    generations: GenerationService
    generation_api: GenerationApiService
    generation_api_keys: GenerationApiKeyService
    conversations: ConversationService
    runtime_logs: RuntimeLogService
    settings: SystemSettingsService
    configuration: RuntimeConfigService
