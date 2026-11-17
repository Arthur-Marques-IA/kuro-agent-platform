import { cn } from "@/lib/cn";
import { initials } from "@/lib/format";

const PALETTE = [
  "bg-indigo-500/15 text-indigo-700 dark:text-indigo-300",
  "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300",
  "bg-amber-500/15 text-amber-700 dark:text-amber-300",
  "bg-rose-500/15 text-rose-700 dark:text-rose-300",
  "bg-sky-500/15 text-sky-700 dark:text-sky-300",
  "bg-violet-500/15 text-violet-700 dark:text-violet-300",
  "bg-teal-500/15 text-teal-700 dark:text-teal-300",
  "bg-orange-500/15 text-orange-700 dark:text-orange-300",
];

const SIZES = {
  xs: "size-5 rounded-md text-[9px]",
  sm: "size-7 rounded-lg text-[11px]",
  md: "size-9 rounded-lg text-xs",
  lg: "size-12 rounded-xl text-sm",
};

function hash(value: string) {
  let h = 0;
  for (const char of value) h = (h * 31 + char.charCodeAt(0)) >>> 0;
  return h;
}

/** Avatar determinístico por slug — o mesmo agente tem a mesma cor em toda a aplicação. */
export function AgentAvatar({
  agentType,
  name,
  size = "md",
  className,
}: {
  agentType: string;
  name?: string;
  size?: keyof typeof SIZES;
  className?: string;
}) {
  return (
    <span
      aria-hidden
      className={cn(
        "inline-flex shrink-0 select-none items-center justify-center font-semibold",
        PALETTE[hash(agentType) % PALETTE.length],
        SIZES[size],
        className
      )}
    >
      {initials(name || agentType, agentType)}
    </span>
  );
}
