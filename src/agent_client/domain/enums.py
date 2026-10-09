from enum import StrEnum


class ApprovalMode(StrEnum):
    ASK = "ask"
    NEVER = "never"
    READ_ONLY = "read_only"


class ErrorCode(StrEnum):
    ACCOUNT_MISMATCH = "account_mismatch"
    API_KEY_MISSING = "api_key_missing"
    ARTIFACT_CORRUPT = "artifact_corrupt"
    ARTIFACT_MISSING = "artifact_missing"
    AUTH_DENIED = "auth_denied"
    AUTH_INVALID = "auth_invalid"
    AUTH_TIMEOUT = "auth_timeout"
    AUTH_UNAVAILABLE = "auth_unavailable"
    BACKUP_EXISTS = "backup_exists"
    BACKUP_PATH = "backup_path"
    BROWSER_UNAVAILABLE = "browser_unavailable"
    COMMAND_CONFLICT = "command_conflict"
    COMMAND_IN_PROGRESS = "command_in_progress"
    COMMAND_INVALID = "command_invalid"
    CONFIG_INVALID = "config_invalid"
    CONTEXT_BUDGET = "context_budget"
    CONTEXT_OVERFLOW = "context_overflow"
    CONTEXT_WINDOW_EXCEEDED = "context_window_exceeded"
    CREDENTIAL_STORE = "credential_store"
    EVENT_CONFLICT = "event_conflict"
    HOST_ID_INVALID = "host_id_invalid"
    IDENTITY_INVALID = "identity_invalid"
    INTERNAL_ERROR = "internal_error"
    INVALID_ARTIFACT = "invalid_artifact"
    INVALID_CONTEXT = "invalid_context"
    INVALID_RESOLUTION = "invalid_resolution"
    INVALID_SESSION = "invalid_session"
    INVALID_SUMMARY = "invalid_summary"
    JOURNAL_CORRUPT = "journal_corrupt"
    JOURNAL_TYPE = "journal_type"
    JOURNAL_VERSION = "journal_version"
    MODEL_ACCESS_DENIED = "model_access_denied"
    MODEL_CATALOG_INVALID = "model_catalog_invalid"
    MODEL_INCOMPLETE = "model_incomplete"
    MODEL_OUTCOME_UNKNOWN = "model_outcome_unknown"
    MODEL_PROTOCOL = "model_protocol"
    MODEL_QUOTA_EXHAUSTED = "model_quota_exhausted"
    MODEL_UNAVAILABLE = "model_unavailable"
    PROJECTION_CONFLICT = "projection_conflict"
    REAUTH_REQUIRED = "reauth_required"
    REFRESH_NOT_READY = "refresh_not_ready"
    REGISTRATION_INVALID = "registration_invalid"
    SCOPE_CHANGED = "scope_changed"
    SCOPE_MISSING = "scope_missing"
    SESSION_BUSY = "session_busy"
    SESSION_EXISTS = "session_exists"
    SESSION_MISSING = "session_missing"
    TOOL_PROTOCOL = "tool_protocol"
    UNKNOWN_OUTCOME = "unknown_outcome"
    UNSUPPORTED_COMPACTION = "unsupported_compaction"
    WORKSPACE_MISSING = "workspace_missing"


class AuthMode(StrEnum):
    CHATGPT = "chatgpt"
    API_KEY = "api_key"


class ProviderKind(StrEnum):
    OPENAI_RESPONSES = "openai_responses"
    OPENAI_CHAT_COMPLETIONS = "openai_chat_completions"


class ChatReasoningMode(StrEnum):
    DEFAULT = "default"
    ENABLED = "enabled"
    DISABLED = "disabled"


class ReasoningEffort(StrEnum):
    NONE = "none"
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"
    ULTRA = "ultra"


class RunStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class ToolStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"
    DENIED = "denied"
    RUNNING = "running"


class ToolExecutionState(StrEnum):
    PROPOSED = "PROPOSED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    READY = "READY"
    DISPATCHING = "DISPATCHING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"
    DENIED = "DENIED"


class StopReason(StrEnum):
    COMPLETED = "completed"
    INTERNAL_ERROR = "internal_error"
    CANCELLED = "cancelled"
    DEADLINE = "deadline"
    MODEL_BUDGET = "model_budget"
    TOOL_BUDGET = "tool_budget"
    BACKGROUND_PROCESS_RUNNING = "background_process_running"
    UNKNOWN_OUTCOME = "unknown_outcome"
    PROCESS_INTERRUPTED = "process_interrupted"
    INCOMPLETE = "incomplete"
    FAILED = "failed"


class ModelEventKind(StrEnum):
    TEXT_DELTA = "text_delta"
    REASONING_DELTA = "reasoning_delta"
    COMPLETED = "completed"


class ReasoningChannel(StrEnum):
    SUMMARY = "summary"
    TEXT = "text"


class ReasoningSummary(StrEnum):
    AUTO = "auto"


class ModelResponseStatus(StrEnum):
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"
    FAILED = "failed"


class RuntimeEventKind(StrEnum):
    INPUT_STEERED = "input_steered"
    CONTEXT_USAGE = "context_usage"
    TEXT_DELTA = "text_delta"
    REASONING_DELTA = "reasoning_delta"
    TOOL_DISPATCHING = "tool_dispatching"
    TOOL_OUTPUT_CHUNK = "tool_output_chunk"
    TOOL_OUTPUT = "tool_output"
    TOOL_FINISHED = "tool_finished"
    MODEL_REQUEST_STARTED = "model_request_started"
    MODEL_COMPLETED = "model_completed"
    COMPACTION_STARTED = "compaction_started"
    COMPACTION_FINISHED = "compaction_finished"
    RUN_FINISHED = "run_finished"
    ERROR = "error"


class JournalEventType(StrEnum):
    SESSION_CREATED = "SessionCreated"
    PENDING_INPUT = "PendingInput"
    USER_MESSAGE = "UserMessage"
    RUN_STARTED = "RunStarted"
    RUN_FINISHED = "RunFinished"
    TOOL_CALL_STATE = "ToolCallState"
    TOOL_RESULT_COMMITTED = "ToolResultCommitted"
    APPROVAL_DECIDED = "ApprovalDecided"
    COMPACTION_STARTED = "CompactionStarted"
    COMPACTION_COMMITTED = "CompactionCommitted"
    MODEL_REQUEST_STARTED = "ModelRequestStarted"
    MODEL_REQUEST_FAILED = "ModelRequestFailed"
    MODEL_RESPONSE_COMMITTED = "ModelResponseCommitted"
    MODEL_RESPONSE_INCOMPLETE = "ModelResponseIncomplete"
    UNKNOWN_OUTCOME_RESOLVED = "UnknownOutcomeResolved"
    BACKGROUND_PROCESS_SETTLED = "BackgroundProcessSettled"
    OBSERVATION = "Observation"


class ContextStrategy(StrEnum):
    SUMMARY = "summary"
    PROVIDER_NATIVE = "provider_native"


class CompactionReason(StrEnum):
    MANUAL = "manual"
    AUTOMATIC = "automatic"
    PROVIDER_OVERFLOW = "provider_overflow"


class NativeItemType(StrEnum):
    FUNCTION_CALL = "function_call"
    FUNCTION_CALL_OUTPUT = "function_call_output"
    REASONING = "reasoning"
    MESSAGE = "message"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    DEVELOPER = "developer"


class TokenMeasurement(StrEnum):
    PROVIDER_INPUT_TOKENS = "provider_input_tokens"
    UTF8_BYTE_ESTIMATE = "utf8_byte_estimate"
    PROVIDER_USAGE_WITH_ESTIMATE = "provider_usage_with_estimate"
    UNAVAILABLE = "unavailable"


class McpTransport(StrEnum):
    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable_http"
