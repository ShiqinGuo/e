from agent_client.domain.enums import ErrorCode


class AgentError(Exception):
    def __init__(self, code: ErrorCode, message: str, *, retryable: bool = False):
        if not isinstance(code, ErrorCode):
            raise TypeError("Error code must be an ErrorCode member")
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
