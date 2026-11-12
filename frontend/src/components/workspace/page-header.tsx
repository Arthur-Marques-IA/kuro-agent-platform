import { Fragment, ReactNode } from "react";
import Link from "next/link";
import { ChevronRight } from "lucide-react";
import { cn } from "@/lib/cn";
import { SidebarTrigger } from "@/components/workspace/app-shell";

export function PageHeader({
  title,
  description,
  actions,
  breadcrumbs,
  tabs,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  breadcrumbs?: { label: string; href?: string }[];
  /** Abas alinhadas à borda inferior do cabeçalho. */
  tabs?: ReactNode;
}) {
  return (
    <header className="shrink-0 border-b border-border bg-background">
      <div className={cn("mx-auto flex w-full max-w-6xl flex-col px-4 pt-5 sm:px-6 lg:px-8", tabs ? "gap-4" : "pb-5")}>
        <div className="flex flex-wrap items-start gap-x-4 gap-y-3">
          <SidebarTrigger className="mt-0.5" />
          <div className="flex min-w-0 flex-1 flex-col gap-1">
            {breadcrumbs && breadcrumbs.length > 0 && (
              <nav aria-label="Trilha" className="flex min-w-0 items-center gap-1 text-[13px] text-muted-foreground">
                {breadcrumbs.map((crumb, i) => (
                  <Fragment key={`${crumb.label}-${i}`}>
                    {i > 0 && <ChevronRight className="pointer-events-none size-3.5 shrink-0 opacity-60" aria-hidden />}
                    {crumb.href ? (
                      // `truncate` corta um ::after; a área de toque de 40 px vem do padding,
                      // compensado pela margem negativa (o desenho da trilha não muda). `relative`
                      // põe o link acima do título, que senão roubaria a parte de baixo do toque.
                      <Link
                        href={crumb.href}
                        className="relative truncate rounded-md hover:text-foreground max-md:-mx-1.5 max-md:-my-2.5 max-md:min-h-10 max-md:min-w-10 max-md:px-1.5 max-md:py-2.5 max-md:text-center"
                      >
                        {crumb.label}
                      </Link>
                    ) : (
                      <span className="truncate text-foreground/70">{crumb.label}</span>
                    )}
                  </Fragment>
                ))}
              </nav>
            )}
            <h1 className="min-w-0 text-xl font-semibold tracking-tight">{title}</h1>
            {description && <div className="text-sm text-muted-foreground">{description}</div>}
          </div>
          {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
        </div>
        {tabs && <div className="-mb-px">{tabs}</div>}
      </div>
    </header>
  );
}

export function PageBody({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div className="scrollbar-thin min-h-0 flex-1 overflow-y-auto">
      <div className={cn("mx-auto w-full max-w-6xl px-4 py-6 sm:px-6 lg:px-8", className)}>{children}</div>
    </div>
  );
}
