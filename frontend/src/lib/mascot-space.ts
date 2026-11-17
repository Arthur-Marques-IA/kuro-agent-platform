/**
 * Telas com controle fixo no rodapé, bem onde o Kuro mora (canto inferior esquerdo da área de
 * conteúdo), pedem que ele saia enquanto estão montadas — ex.: a barra "Alterações não salvas"
 * do formulário de agente. Um contador, não um booleano: duas telas pedindo e uma saindo ainda
 * deixa o espaço reservado. Módulo puro, sem React, para rodar no `npm test`.
 */

export interface Reservations {
  /** Reserva o espaço; a função devolvida libera (chamar de novo não faz nada). */
  reserve(): () => void;
  isReserved(): boolean;
  subscribe(listener: () => void): () => void;
}

export function createReservations(): Reservations {
  let count = 0;
  const listeners = new Set<() => void>();
  const notify = () => {
    for (const listener of listeners) listener();
  };

  return {
    reserve() {
      count += 1;
      if (count === 1) notify();
      let released = false;
      return () => {
        if (released) return;
        released = true;
        count -= 1;
        if (count === 0) notify();
      };
    },
    isReserved() {
      return count > 0;
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  };
}

/** O espaço do Kuro no console. */
export const mascotSpace = createReservations();
