"""Submarine plugin for Sublime Text.

Root entry point. ST only auto-loads ROOT ``*.py`` files, so every command
class and listener must be imported here (or via another root module).
"""
from __future__ import annotations

import os
import sys

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from main import (  # noqa: F401
    plugin_loaded,
    plugin_unloaded,
    get_session_for_view,
    get_active_session,
    create_session,
    in_startup_quiet,
    construct_session,
    schedule_auto_sleep,
)

import importlib
import commands.session_cmds as _session_cmds
try:
    importlib.reload(_session_cmds)
except Exception:
    pass
from commands.session_cmds import SubmarineToggleListCommand  # noqa: F401
try:
    from commands.session_cmds import SubmarineCycleSessionCommand  # noqa: F401
except ImportError:
    SubmarineCycleSessionCommand = getattr(
        _session_cmds, "SubmarineCycleSessionCommand", None)
from commands import (  # noqa: F401
    SubmarineStartCommand,
    CodexStartCommand,
    DeepSeekStartCommand,
    StepFunStartCommand,
    PiStartCommand,
    GrokStartCommand,
    KimiStartCommand,
    SubmarineQueryCommand,
    SubmarineRestartCommand,
    SubmarineRestartNewCommand,
    SubmarineCopyAgentIdCommand,
    SubmarineCopySessionIdCommand,
    SubmarineQueuePromptCommand,
    SubmarineInterruptCommand,
    SubmarineCloseSessionCommand,
    SubmarineRenameCommand,
    SubmarineToggleCommand,
    SubmarineStopCommand,
    SubmarineTearOffSessionCommand,
    SubmarineDockSessionCommand,
    SubmarineHideSessionCommand,
    SubmarineRevealSessionCommand,
    SubmarineSleepSessionCommand,
    SubmarineWakeSessionCommand,
    SubmarineToggleAutoSleepCommand,
    SubmarineResumeCommand,
    SubmarineSwitchCommand,
    SubmarineForkCommand,
    SubmarineForkFromCommand,
    SubmarineSessionListCommand,
    SubmarineSessionListRefreshCommand,
    SubmarineSessionListOpenCommand,
    SubmarineManageProvidersCommand,
    SubmarineStartCustomProviderCommand,
    SubmarineGenerateProviderModelsCommand,
    SubmarineChangeProviderCommand,
    SubmarineSelectEffortCommand,
    SubmarineSetDefaultEffortCommand,
    SubmarineSelectModelCommand,
    SubmarineSetDefaultModelCommand,
    SubmarineSetDefaultProviderCommand,
    SubmarineRefreshModelsCommand,
    SubmarineQuerySelectionCommand,
    SubmarineQueryFileCommand,
    SubmarineAddFileCommand,
    SubmarineAddSelectionCommand,
    SubmarineAddOpenFilesCommand,
    SubmarineAddFolderCommand,
    SubmarineClearContextCommand,
    SubmarineClearCommand,
    SubmarineClearKeepLastCommand,
    SubmarineCopyCommand,
    SubmarineUsageCommand,
    SubmarineSearchSessionsCommand,
    SubmarineViewHistoryCommand,
    SubmarineResetInputCommand,
    SubmarineAddMcpCommand,
    SubmarineTogglePermissionModeCommand,
    SubmarineDevtoolsSnapshotCommand,
    SubmarineDevtoolsSessionsCommand,
    SubmarineDevtoolsComposerCommand,
    SubmarineDevtoolsLogCommand,
    SubmarineDevtoolsReloadCommand,
    SubmarineArtifactsCommand,
    SubmarineToggleSubmitModeCommand,
    SubmarineSendNowCommand,
    SubmarineSubmitInputCommand,
    SubmarineGoalStatusCommand,
    SubmarineGoalPauseCommand,
    SubmarineGoalResumeCommand,
    SubmarineGoalClearCommand,
    SubmarineInsertCommand,
    SubmarineToggleTasksFoldCommand,
    SubmarineReplaceCommand,
    SubmarineReplaceContentCommand,
    SubmarineClearAllCommand,
    SubmarineUndoClearCommand,
    SubmarineInsertNewlineCommand,
    NoopCommand,
    SubmarineSelectDraftCommand,
    SubmarineSelectHistoryCommand,
    SubmarinePermissionAllowCommand,
    SubmarinePermissionDenyCommand,
    SubmarineUndoMessageCommand,
    SubmarineViewPlanCommand,
    SubmarinePermissionAllowSessionCommand,
    SubmarinePermissionAllowAllCommand,
    SubmarineQuestionKeyCommand,
    SubmarineQuickPromptCommand,
    SubmarineManageAutoAllowedToolsCommand,
    SubmarinePasteImageCommand,
    SubmarineOpenLinkCommand,
    SubmarineRetainCommand,
    SubmarineProjectRetainCommand,
    SubmarineSessionJsonlCommand,
    SubmarineSessionListSetTextCommand,
    SubmarineSessionListCloseCommand,
    SubmarineSessionListRenameCommand,
    SubmarineSessionListForkCommand,
    SubmarineSessionListJsonlCommand,
    SubmarineSessionListStarCommand,
    SubmarineSessionListRevealCommand,
    SubmarineSessionListTearOffCommand,
)

from ui.listeners import (  # noqa: F401
    SubmarineEventListener,
    SubmarineOutputEventListener,
)
from ui.session_list import SessionListClickListener  # noqa: F401
