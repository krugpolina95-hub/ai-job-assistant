import { z } from "zod";

const serverEnvSchema = z.object({
  BACKEND_INTERNAL_URL: z.url({ protocol: /^https?$/ }).transform((url) => url.replace(/\/+$/, "")),
});

export type ServerEnv = z.infer<typeof serverEnvSchema>;

export function parseServerEnv(source: Record<string, string | undefined>): ServerEnv {
  const result = serverEnvSchema.safeParse(source);
  if (!result.success) {
    throw new Error(`Invalid frontend environment:\n${z.prettifyError(result.error)}`);
  }
  return result.data;
}

export function getServerEnv(): ServerEnv {
  return parseServerEnv(process.env);
}
