import type { Metadata } from "next";
import { ToolsIndex } from "@/components/tools/tools-index";

export const metadata: Metadata = { title: "Tools" };

/** `?q=<texto>` abre a lista já filtrada — a busca global (`Ctrl+K`) leva a uma tool assim. */
export default async function ToolsPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const { q } = await searchParams;
  const initialQuery = typeof q === "string" ? q : "";
  return <ToolsIndex key={initialQuery} initialQuery={initialQuery} />;
}
