"use client";

import { useEffect, useState } from "react";
import { subscribeAnnouncements } from "@/lib/announce";

/**
 * As duas regiões vivas do console (educada e assertiva), montadas uma vez no layout raiz.
 * Quem quer falar chama `announce()` de `@/lib/announce`. As regiões existem desde o
 * primeiro render, vazias: leitor de tela só lê mudança em região que já estava lá.
 */
export function LiveAnnouncer() {
  const [polite, setPolite] = useState("");
  const [assertive, setAssertive] = useState("");

  useEffect(() => {
    const timers: number[] = [];
    const unsubscribe = subscribeAnnouncements(({ text, assertive: urgent, id }) => {
      const set = urgent ? setAssertive : setPolite;
      // Esvazia e escreve no quadro seguinte: o mesmo texto repetido volta a ser lido.
      // O sufixo invisível (id par/ímpar) garante que o texto mude mesmo assim.
      set("");
      timers.push(window.setTimeout(() => set(id % 2 ? text : `${text} `), 60));
    });
    return () => {
      unsubscribe();
      timers.forEach((t) => window.clearTimeout(t));
    };
  }, []);

  return (
    <>
      <div role="status" aria-live="polite" aria-atomic="true" className="sr-only" data-announcer="polite">
        {polite}
      </div>
      <div aria-live="assertive" aria-atomic="true" className="sr-only" data-announcer="assertive">
        {assertive}
      </div>
    </>
  );
}
