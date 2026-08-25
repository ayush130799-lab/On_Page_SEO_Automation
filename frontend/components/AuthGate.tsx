"use client";

/**
 * Authentication shell.
 *
 * Holds the current user in context, redirects to `/login` when there is no valid session, and
 * renders the application chrome. Every authenticated screen wraps itself in `<AuthGate>` so no
 * page has to reimplement the redirect.
 */

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";

import { ApiError, api, tokens } from "@/lib/api";
import type { User } from "@/lib/types";

import { Spinner } from "./ui";

interface AuthContextValue {
  user: User | null;
  logout: () => void;
  refresh: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue>({
  user: null,
  logout: () => undefined,
  refresh: async () => undefined,
});

export const useAuth = () => useContext(AuthContext);

export function AuthGate({ children }: { children: ReactNode }) {
  const router = useRouter();
  const [user, setUser] = useState<User | null>(null);
  const [status, setStatus] = useState<"checking" | "ready" | "anonymous">("checking");

  const load = useCallback(async () => {
    if (!tokens.isAuthenticated) {
      setStatus("anonymous");
      return;
    }
    try {
      setUser(await api.auth.me());
      setStatus("ready");
    } catch (error) {
      if (error instanceof ApiError && error.isAuthError) {
        tokens.clear();
        setStatus("anonymous");
      } else {
        // A backend outage should not bounce the user to the login screen: keep them on the page
        // so the error is visible and a retry works.
        setStatus("ready");
      }
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (status === "anonymous") router.replace("/login");
  }, [status, router]);

  const logout = useCallback(() => {
    api.auth.logout();
    setUser(null);
    router.replace("/login");
  }, [router]);

  if (status === "checking") {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Spinner label="Checking your session…" />
      </div>
    );
  }

  if (status === "anonymous") return null;

  return (
    <AuthContext.Provider value={{ user, logout, refresh: load }}>
      <div className="min-h-screen">
        <TopBar user={user} onLogout={logout} />
        <main className="mx-auto w-full max-w-[1600px] px-4 py-6 sm:px-6 lg:px-8">{children}</main>
      </div>
    </AuthContext.Provider>
  );
}

function TopBar({ user, onLogout }: { user: User | null; onLogout: () => void }) {
  const pathname = usePathname();

  const links = [
    { href: "/", label: "Portfolio" },
    { href: "/websites/new", label: "Add website" },
    { href: "/settings", label: "Settings" },
  ];

  return (
    <header className="sticky top-0 z-20 border-b border-slate-800 bg-slate-950/90 backdrop-blur">
      <div className="mx-auto flex w-full max-w-[1600px] items-center gap-6 px-4 py-3 sm:px-6 lg:px-8">
        <Link href="/" className="flex items-center gap-2 text-sm font-semibold text-slate-100">
          <span className="grid h-7 w-7 place-items-center rounded-lg bg-sky-500 text-slate-950">
            SE
          </span>
          SEO Automation
        </Link>

        <nav className="flex items-center gap-1 text-sm">
          {links.map((link) => {
            const active =
              link.href === "/" ? pathname === "/" : pathname?.startsWith(link.href);
            return (
              <Link
                key={link.href}
                href={link.href}
                className={`rounded-lg px-3 py-1.5 transition-colors ${
                  active
                    ? "bg-slate-800 text-slate-100"
                    : "text-slate-400 hover:bg-slate-900 hover:text-slate-200"
                }`}
              >
                {link.label}
              </Link>
            );
          })}
        </nav>

        <div className="ml-auto flex items-center gap-3 text-sm">
          {user && (
            <span className="hidden text-slate-400 sm:inline">
              {user.email}
              {user.role === "admin" && (
                <span className="ml-2 chip bg-sky-500/15 text-sky-300 ring-sky-500/30">admin</span>
              )}
            </span>
          )}
          <button type="button" onClick={onLogout} className="btn-ghost">
            Sign out
          </button>
        </div>
      </div>
    </header>
  );
}
