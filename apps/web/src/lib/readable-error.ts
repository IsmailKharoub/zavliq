/** Keep SDK request URLs and transport details out of user-facing notices. */
export function readableError(error: unknown): string {
  if (error && typeof error === "object" && "errcode" in error) {
    switch (error.errcode) {
      case "M_TOO_LARGE":
        return "The message is too large after formatting or encryption. Shorten it or send the content as a file.";
      case "M_LIMIT_EXCEEDED":
        return "A service limit was reached. Check your usage before retrying the saved attempt.";
      case "M_FORBIDDEN":
        return "The service refused this action. Check your conversation permissions and service limits.";
      case "M_UNKNOWN_TOKEN":
      case "M_MISSING_TOKEN":
        return "This device's session is no longer accepted. Reconnect using pairing or account recovery.";
      default:
        return "The service could not complete this action. Retry when the connection is available.";
    }
  }
  return error instanceof Error ? error.message : "Something went wrong. Please try again.";
}
