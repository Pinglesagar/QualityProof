/**
 * Redaction rules mirrored from the Python engine's `security.py`.
 *
 * The rules are duplicated deliberately rather than shared over a wire: a
 * reporter that had to call out to Python to sanitise its own output could not
 * guarantee redaction happened before the manifest hit disk. The interop tests
 * assert both implementations agree on the same fixtures, so the duplication is
 * verified rather than assumed.
 */

const SECRET_ENV_NAME =
  /(?:PASSWORD|SECRET|TOKEN|API_KEY|APIKEY|CREDENTIAL|AUTHORIZATION|COOKIE|USERNAME|USER_EMAIL|LOGIN)/i;
const SENSITIVE_KEY = /(?:authorization|cookie|credential|password|secret|token|api[_-]?key)/i;
const BEARER = /\bBearer\s+[A-Za-z0-9._~+/=-]+/gi;
const BASIC = /\bBasic\s+[A-Za-z0-9+/=]+/gi;
const URL_CREDENTIAL = /(https?:\/\/)[^/@\s:]+(?::[^/@\s]*)?@/gi;

/** Below this length a value is too generic to substitute safely. */
export const MIN_REDACTABLE_LENGTH = 6;
export const REDACTED = "<REDACTED>";

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export class Redactor {
  private readonly long: string[];
  private readonly short: string[];

  constructor(secrets: readonly string[] = []) {
    const unique = [...new Set(secrets.filter((value) => value.length > 0))].sort(
      (a, b) => b.length - a.length,
    );
    this.long = unique.filter((value) => value.length >= MIN_REDACTABLE_LENGTH);
    this.short = unique.filter((value) => value.length < MIN_REDACTABLE_LENGTH);
  }

  static fromEnvironment(env: NodeJS.ProcessEnv = process.env): Redactor {
    const values: string[] = [];
    for (const [name, value] of Object.entries(env)) {
      if (value && SECRET_ENV_NAME.test(name)) {
        values.push(value);
      }
    }
    return new Redactor(values);
  }

  get secrets(): readonly string[] {
    return [...this.long, ...this.short];
  }

  text(value: string): string {
    let redacted = value;
    for (const secret of this.long) {
      redacted = redacted.split(secret).join(REDACTED);
    }
    for (const secret of this.short) {
      // Word-bounded so a short value cannot shred unrelated text.
      redacted = redacted.replace(new RegExp(`\\b${escapeRegExp(secret)}\\b`, "g"), REDACTED);
    }
    redacted = redacted.replace(BEARER, `Bearer ${REDACTED}`);
    redacted = redacted.replace(BASIC, `Basic ${REDACTED}`);
    return redacted.replace(URL_CREDENTIAL, `$1${REDACTED}@`);
  }

  value(input: unknown): unknown {
    if (Array.isArray(input)) {
      return input.map((item) => this.value(item));
    }
    if (input && typeof input === "object") {
      return Object.fromEntries(
        Object.entries(input as Record<string, unknown>).map(([key, nested]) => [
          key,
          SENSITIVE_KEY.test(key) ? REDACTED : this.value(nested),
        ]),
      );
    }
    if (typeof input === "string") {
      return this.text(input);
    }
    return input;
  }
}

/** True when the environment exposes anything that must not reach an artifact. */
export function environmentIsAuthenticated(env: NodeJS.ProcessEnv = process.env): boolean {
  if (env.QUALITYPROOF_STORAGE_STATE) {
    return true;
  }
  return Object.entries(env).some(([name, value]) => Boolean(value) && SECRET_ENV_NAME.test(name));
}
