"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { AuthGate } from "@/components/AuthGate";
import { Card, ErrorNote, PageHeader } from "@/components/ui";
import { ApiError, api } from "@/lib/api";

export default function NewWebsitePage() {
  return (
    <AuthGate>
      <NewWebsiteForm />
    </AuthGate>
  );
}

function NewWebsiteForm() {
  const router = useRouter();
  const [form, setForm] = useState({
    name: "",
    url: "",
    github_repo: "",
    github_branch: "main",
    github_framework: "",
    max_pages: "",
    render_mode: "auto",
    exclude_patterns: "",
  });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const update = (key: keyof typeof form, value: string) =>
    setForm((current) => ({ ...current, [key]: value }));

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");

    try {
      const website = await api.websites.create({
        name: form.name.trim(),
        url: form.url.trim(),
        github_repo: form.github_repo.trim() || null,
        github_branch: form.github_branch.trim() || "main",
        github_framework: form.github_framework || null,
        max_pages: form.max_pages ? Number(form.max_pages) : null,
        render_mode: form.render_mode,
        exclude_patterns: form.exclude_patterns
          ? form.exclude_patterns
              .split("\n")
              .map((line) => line.trim())
              .filter(Boolean)
          : null,
      });
      router.push(`/websites/${website.id}/integrations`);
    } catch (caught) {
      if (caught instanceof ApiError) {
        const details = Array.isArray(caught.details)
          ? (caught.details as { field?: string; message?: string }[])
              .map((detail) => `${detail.field}: ${detail.message}`)
              .join("; ")
          : "";
        setError(details ? `${caught.message} ${details}` : caught.message);
      } else {
        setError("Could not create the website.");
      }
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto max-w-2xl">
      <PageHeader
        breadcrumb={[{ href: "/", label: "Portfolio" }]}
        title="Add a website"
        subtitle="Register a site your company builds. You can connect its data sources next."
      />

      <form onSubmit={submit} className="space-y-4">
        {error && <ErrorNote error={error} />}

        <Card title="Website">
          <div className="space-y-4">
            <div>
              <label className="label" htmlFor="name">
                Name
              </label>
              <input
                id="name"
                required
                className="input"
                placeholder="Acme Marketing Site"
                value={form.name}
                onChange={(event) => update("name", event.target.value)}
              />
            </div>

            <div>
              <label className="label" htmlFor="url">
                URL
              </label>
              <input
                id="url"
                required
                type="url"
                className="input"
                placeholder="https://acme.com"
                value={form.url}
                onChange={(event) => update("url", event.target.value)}
              />
              <p className="mt-1.5 text-xs text-slate-500">
                The crawl starts here and stays on this domain.
              </p>
            </div>
          </div>
        </Card>

        <Card title="Crawl settings">
          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <label className="label" htmlFor="max-pages">
                Page limit
              </label>
              <input
                id="max-pages"
                type="number"
                min={1}
                className="input"
                placeholder="Default (5000)"
                value={form.max_pages}
                onChange={(event) => update("max_pages", event.target.value)}
              />
            </div>

            <div>
              <label className="label" htmlFor="render-mode">
                JavaScript rendering
              </label>
              <select
                id="render-mode"
                className="input"
                value={form.render_mode}
                onChange={(event) => update("render_mode", event.target.value)}
              >
                <option value="auto">Auto — render only thin pages</option>
                <option value="always">Always render (slow)</option>
                <option value="never">Never render (fastest)</option>
              </select>
            </div>

            <div className="sm:col-span-2">
              <label className="label" htmlFor="exclude">
                Exclude paths
              </label>
              <textarea
                id="exclude"
                rows={3}
                className="input font-mono text-xs"
                placeholder={"/admin/*\n/cart*\n/search*"}
                value={form.exclude_patterns}
                onChange={(event) => update("exclude_patterns", event.target.value)}
              />
              <p className="mt-1.5 text-xs text-slate-500">
                One glob per line. Use <code>*</code> as the wildcard.
              </p>
            </div>
          </div>
        </Card>

        <Card title="GitHub (optional)">
          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <label className="label" htmlFor="repo">
                Repository
              </label>
              <input
                id="repo"
                className="input"
                placeholder="acme/website"
                value={form.github_repo}
                onChange={(event) => update("github_repo", event.target.value)}
              />
            </div>

            <div>
              <label className="label" htmlFor="branch">
                Branch
              </label>
              <input
                id="branch"
                className="input"
                value={form.github_branch}
                onChange={(event) => update("github_branch", event.target.value)}
              />
            </div>

            <div className="sm:col-span-2">
              <label className="label" htmlFor="framework">
                Framework
              </label>
              <select
                id="framework"
                className="input"
                value={form.github_framework}
                onChange={(event) => update("github_framework", event.target.value)}
              >
                <option value="">Detect automatically</option>
                <option value="next">Next.js</option>
                <option value="nuxt">Nuxt</option>
                <option value="astro">Astro</option>
                <option value="sveltekit">SvelteKit</option>
                <option value="remix">Remix</option>
                <option value="gatsby">Gatsby</option>
                <option value="hugo">Hugo</option>
                <option value="jekyll">Jekyll</option>
                <option value="static">Static HTML</option>
              </select>
              <p className="mt-1.5 text-xs text-slate-500">
                Used to map changed source files to the pages they affect, so a push re-audits only
                what it touched.
              </p>
            </div>
          </div>
        </Card>

        <div className="flex justify-end gap-2">
          <button type="button" onClick={() => router.back()} className="btn-secondary">
            Cancel
          </button>
          <button type="submit" disabled={busy} className="btn-primary">
            {busy ? "Creating…" : "Create website"}
          </button>
        </div>
      </form>
    </div>
  );
}
