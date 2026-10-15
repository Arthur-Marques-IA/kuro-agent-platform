"""Fila de jobs no próprio Postgres: a tabela e as transições (`store`), e o worker
que roda no processo do serviço (`worker`). Para um tipo novo de trabalho em
background: `@worker.register("kind")` num handler e `store.enqueue("kind", payload)`."""
