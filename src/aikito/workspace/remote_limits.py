"""Internal capacity policy, independent of protocol and encoding versions.

A single resource is bounded by its portable payload encoding so that one
resource, after Base64 and envelope overhead, always fits a request or response.
"""

MAX_REMOTE_REQUEST_BYTES = 64 * 1024 * 1024
MAX_REMOTE_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_RESOURCE_PAYLOAD_BYTES = 16 * 1024 * 1024
