"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";

import { ApiError, api, tokens } from "@/lib/api";
import { ErrorNote, Spinner } from "@/components/ui";

export default function LoginPage() {
  const router = useRouter();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [fullName, setFullName] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [registrationEnabled, setRegistrationEnabled] = useState(true);

  useEffect(() => {
    if (tokens.isAuthenticated) router.replace("/");
  }, [router]);

  useEffect(() => {
    api.auth
      .config()
      .then((config) => setRegistrationEnabled(config.registration_enabled))
      .catch(() => setRegistrationEnabled(true));
  }, []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    setNotice("");
    setBusy(true);

    try {
      if (mode === "register") {
        await api.auth.register(email, password, fullName);
        // Registering does not sign you in, so log in immediately with the same credentials.
        await api.auth.login(email, password);
      } else {
        await api.auth.login(email, password);
      }
      router.replace("/");
    } catch (caught) {
      if (caught instanceof ApiError) {
        const details = Array.isArray(caught.details)
          ? (caught.details as { message?: string }[])
              .map((detail) => detail.message)
              .filter(Boolean)
              .join(" ")
          : "";
        setError(details ? `${caught.message} ${details}` : caught.message);
      } else {
        setError("Something went wrong. Please try again.");
      }
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-4 py-12">
      <div className="w-full max-w-sm">
        <div className="mb-8 text-center">
          <div className="mx-auto mb-3 grid h-11 w-11 place-items-center rounded-xl bg-sky-500 text-lg font-bold text-slate-950">
            SE
          </div>
          <h1 className="text-lg font-semibold text-slate-100">SEO Automation</h1>
          <p className="mt-1 text-sm text-slate-400">
            {mode === "login"
              ? "Sign in to review your portfolio."
              : "Create an account to get started."}
          </p>
        </div>

        <form onSubmit={submit} className="card space-y-4 p-6">
          {error && <ErrorNote error={error} />}
          {notice && <p className="text-sm text-emerald-300">{notice}</p>}

          {mode === "register" && (
            <div>
              <label className="label" htmlFor="full-name">
                Full name
              </label>
              <input
                id="full-name"
                className="input"
                value={fullName}
                onChange={(event) => setFullName(event.target.value)}
                autoComplete="name"
              />
            </div>
          )}

          <div>
            <label className="label" htmlFor="email">
              Email
            </label>
            <input
              id="email"
              type="email"
              required
              className="input"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              autoComplete="email"
            />
          </div>

          <div>
            <label className="label" htmlFor="password">
              Password
            </label>
            <input
              id="password"
              type="password"
              required
              className="input"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              autoComplete={mode === "login" ? "current-password" : "new-password"}
            />
            {mode === "register" && (
              <p className="mt-1.5 text-xs text-slate-500">
                At least 10 characters, mixing letters with digits or symbols.
              </p>
            )}
          </div>

          <button type="submit" disabled={busy} className="btn-primary w-full">
            {busy ? <Spinner label="" /> : mode === "login" ? "Sign in" : "Create account"}
          </button>

          {registrationEnabled && (
            <p className="text-center text-sm text-slate-400">
              {mode === "login" ? "No account yet?" : "Already have an account?"}{" "}
              <button
                type="button"
                onClick={() => {
                  setMode(mode === "login" ? "register" : "login");
                  setError("");
                }}
                className="text-sky-400 hover:underline"
              >
                {mode === "login" ? "Create one" : "Sign in"}
              </button>
            </p>
          )}
        </form>

        <p className="mt-4 text-center text-xs text-slate-600">
          The first account created becomes the platform administrator.
        </p>
      </div>
    </div>
  );
}
