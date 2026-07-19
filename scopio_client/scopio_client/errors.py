class ScopioError(Exception):
    """Any failure talking to the SCOPIO gateway (HTTP error, timeout,
    rejected WebSocket op, action failure)."""

    def __init__(self, message, status=None, payload=None):
        super().__init__(message)
        self.status = status      # HTTP status code, when applicable
        self.payload = payload    # decoded error body / envelope, when available
