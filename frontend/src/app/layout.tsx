import type { Metadata } from "next";
// Fontes servidas pelo próprio app (@fontsource), sem `next/font/google`: aquele
// baixa do Google durante o build, e o build do CI quebrava sem conseguir resolver.
// Cada arquivo de peso traz todos os subsets com `unicode-range` — o navegador só
// baixa o que a página usa.
import "@fontsource/ibm-plex-sans/400.css";
import "@fontsource/ibm-plex-sans/500.css";
import "@fontsource/ibm-plex-sans/600.css";
import "@fontsource/ibm-plex-sans/700.css";
import "@fontsource/jetbrains-mono/400.css";
import "@fontsource/jetbrains-mono/500.css";
import "@fontsource/jetbrains-mono/600.css";
import { LiveAnnouncer } from "@/components/ui/announcer";
import { ConfirmProvider } from "@/components/ui/confirm";
import { ToastProvider } from "@/components/ui/toast";
import { themeInitScript } from "@/lib/theme-script";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "kuro", template: "%s · kuro" },
  description: "Console do Kuro, runtime de agentes de IA: playground, agentes versionados, base de conhecimento e logs.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="pt-BR"
      className="h-full antialiased"
      suppressHydrationWarning
    >
      <head>
        {/* Aplica o tema salvo antes do primeiro paint (ver lib/theme-script.ts). */}
        <script dangerouslySetInnerHTML={{ __html: themeInitScript }} />
      </head>
      <body className="h-full">
        <ToastProvider>
          <ConfirmProvider>{children}</ConfirmProvider>
        </ToastProvider>
        <LiveAnnouncer />
      </body>
    </html>
  );
}
