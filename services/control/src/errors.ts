export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public action?: string, public retryAfterMs?: number) { super(message); }
}
