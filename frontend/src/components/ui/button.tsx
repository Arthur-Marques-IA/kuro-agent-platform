import { ButtonHTMLAttributes, forwardRef } from "react";
import { cn } from "@/lib/cn";

type Variant = "default" | "secondary" | "outline" | "ghost" | "destructive" | "link";
type Size = "default" | "sm" | "lg" | "icon" | "icon-sm" | "icon-xs";

// `relative touch-target`: em tela de toque ou estreita, todo botão (inclusive os de ícone de
// 28–32 px) ganha área de toque de 40 px sem mudar de tamanho; no desktop nada muda.
const baseClasses =
  "relative touch-target inline-flex shrink-0 select-none items-center justify-center gap-2 whitespace-nowrap rounded-lg font-medium transition-colors " +
  "outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background " +
  "disabled:pointer-events-none disabled:opacity-50 [&_svg]:size-4 [&_svg]:shrink-0";

const variantClasses: Record<Variant, string> = {
  default: "bg-primary text-primary-foreground shadow-xs hover:bg-primary/90",
  secondary: "bg-muted text-foreground hover:bg-accent",
  outline: "border border-input bg-card text-foreground shadow-xs hover:bg-accent",
  ghost: "text-foreground hover:bg-accent",
  destructive: "bg-destructive text-destructive-foreground shadow-xs hover:bg-destructive/90",
  link: "h-auto px-0 text-primary underline-offset-4 hover:underline",
};

const sizeClasses: Record<Size, string> = {
  default: "h-9 px-4 text-sm",
  sm: "h-8 gap-1.5 px-3 text-[13px]",
  lg: "h-10 px-5 text-sm",
  icon: "size-9",
  "icon-sm": "size-8",
  "icon-xs": "size-7 rounded-md [&_svg]:size-3.5",
};

/** Classes do botão, para aplicar o mesmo visual em `<Link>` sem duplicar estilos. */
export function buttonVariants({
  variant = "default",
  size = "default",
  className,
}: { variant?: Variant; size?: Size; className?: string } = {}) {
  return cn(baseClasses, variantClasses[variant], sizeClasses[size], className);
}

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, type = "button", ...props }, ref) => (
    <button ref={ref} type={type} className={buttonVariants({ variant, size, className })} {...props} />
  )
);
Button.displayName = "Button";
