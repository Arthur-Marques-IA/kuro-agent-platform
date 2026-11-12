import { expect, test } from "@playwright/test";

/**
 * O caminho principal do console, de ponta a ponta: criar um agente → conversar
 * no Playground → abrir a sessão nos Logs → abrir o trace da execução.
 *
 * O backend é o agent-service de verdade (`scripts/servico_de_teste.py`) com um
 * modelo determinístico: toda resposta é `Resposta de teste: <mensagem>`. Os
 * seletores são por papel e texto acessível, como quem usa a tela os acha.
 */

const RESPOSTA = (mensagem: string) => `Resposta de teste: ${mensagem}`;

test("criar agente, conversar, abrir a sessão e o trace", async ({ page }) => {
  // Nome único: com `reuseExistingServer` o banco do backend sobrevive entre execuções.
  const sufixo = Date.now().toString(36);
  const nome = `Smoke ${sufixo}`;
  const slug = `smoke-${sufixo}`;
  const mensagem = `olá do smoke ${sufixo}`;

  // 1. Criar o agente
  await page.goto("/agents/new");
  await page.getByLabel("Nome", { exact: true }).fill(nome);
  await expect(page.getByLabel("Slug (agent_type)")).toHaveValue(slug);
  await page.getByLabel("Instruções").fill("Responda em uma frase.");
  // A lista de modelos é lida do backend; o botão só vale depois dela.
  await expect(page.getByLabel("Modelo", { exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "Criar agente" }).click();
  await expect(page).toHaveURL(new RegExp(`/agents/${slug}$`));
  await expect(page.getByRole("heading", { name: nome })).toBeVisible();

  // 2. Conversar no Playground
  await page.getByRole("link", { name: "Conversar", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`/chat\\?agent=${slug}`));
  const composer = page.getByPlaceholder(`Mensagem para ${nome}…`);
  await composer.fill(mensagem);
  await page.getByRole("button", { name: "Enviar mensagem" }).click();
  await expect(page.getByText(RESPOSTA(mensagem))).toBeVisible();
  // O primeiro envio fixa a conversa na URL: /chat/<session_id>.
  await expect(page).toHaveURL(/\/chat\/[^/?]+$/);
  const sessionId = decodeURIComponent(new URL(page.url()).pathname.split("/").pop()!);

  // 3. Abrir a sessão (o painel de detalhes da conversa leva aos Logs)
  const abrirDetalhes = page.getByRole("button", { name: "Abrir detalhes" });
  if (await abrirDetalhes.isVisible()) await abrirDetalhes.click();
  await page.getByRole("link", { name: "Execuções desta conversa" }).click();
  await expect(page).toHaveURL(new RegExp(`/logs/sessions/${sessionId}$`));
  await expect(page.getByText(mensagem).first()).toBeVisible();
  await expect(page.getByText(RESPOSTA(mensagem)).first()).toBeVisible();

  // 4. Abrir o trace da execução
  await page.locator('a[href^="/runs/"]').first().click();
  await expect(page).toHaveURL(/\/runs\/[0-9a-f-]+$/);
  await expect(page.getByRole("heading", { name: /Indexando|não encontrado|Não foi possível/ })).toHaveCount(0, {
    timeout: 30_000,
  });
  await expect(page.getByText(nome).first()).toBeVisible();
  await expect(page.getByText(RESPOSTA(mensagem)).first()).toBeVisible();
});
