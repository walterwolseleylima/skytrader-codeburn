# CodeBurn

Um dashboard simples pra ver quanto de token o seu Claude Code está queimando, projeto por projeto.

Ele lê os logs de sessão que o Claude Code já grava na sua máquina, estima o custo equivalente em dólar e gera uma página HTML com tabelas e gráficos. Tudo local. Nada sai do seu computador.

![CodeBurn](https://img.shields.io/badge/python-3.10%2B-blue) ![deps](https://img.shields.io/badge/depend%C3%AAncias-zero-green)

## Antes de tudo: isso não é a sua fatura

Se você usa Claude Pro ou Max, paga assinatura fixa por mês, não por token. Os valores em dólar que aparecem aqui são o **equivalente se você estivesse pagando via API**. Serve como termômetro de intensidade de uso, ótimo pra comparar um projeto com o outro e enxergar onde o consumo está concentrado. Não trate como conta a pagar.

## Como funciona

O Claude Code guarda cada sessão em arquivos `.jsonl` dentro de `~/.claude/projects/`. O CodeBurn varre esses arquivos, soma os tokens de cada chamada (input, cache de leitura, cache de escrita, output), aplica a tabela de preços e agrupa por projeto, por modelo, por dia e por sessão.

Na primeira vez ele monta um cache local pra ficar rápido nas próximas. Conforme você usa o Claude Code, é só rodar de novo pra atualizar.

## Requisitos

- **Python 3.10 ou mais novo.** Em versões antigas ele nem abre.
- Sem nenhuma biblioteca extra. Só a biblioteca padrão do Python.
- Conexão com a internet pra renderizar os gráficos (eles usam Chart.js via CDN). As tabelas funcionam offline normalmente.

## Como usar

Clone ou baixe a pasta, entre nela e rode:

```bash
python codeburn.py
```

Isso gera o `report.html` ao lado do script. Abra no navegador e pronto.

Outras opções:

```bash
python codeburn.py --days 7      # só os últimos 7 dias
python codeburn.py --open        # gera e já abre no navegador
python codeburn.py --json saida.json   # exporta os dados em JSON também
```

## Deixe do seu jeito

**Agrupar por projeto.** Abra o `codeburn.py` e edite a lista `PROJECT_RULES` lá no topo. Cada linha é um par: uma expressão que casa com o caminho da pasta, e o nome que você quer ver no relatório. A primeira que casar vence. Se nenhuma casar, o relatório usa o próprio caminho da pasta como nome. Os exemplos que vêm no arquivo (Frontend, Backend, Infra) são só ponto de partida, troque pelos seus.

**Ajustar os preços.** A tabela fica no `pricing.json`. Confira os valores atuais na [página de preços da Anthropic](https://www.anthropic.com/pricing) e ajuste se precisar. Tem também o campo `usd_brl` pra converter pra real, mude pra sua moeda se quiser.

## Privacidade

O CodeBurn roda inteiro na sua máquina e lê só os seus próprios logs. Ele não envia nada pra lugar nenhum. A única coisa que sai pela rede é o carregamento da biblioteca de gráficos (Chart.js) a partir de um CDN público, e isso é código, não os seus dados.

O `report.html` gerado contém os nomes dos seus projetos e o seu consumo. O `.gitignore` já está configurado pra você não subir esse relatório nem o cache por acidente, caso você versione a sua cópia.

## Licença

MIT. Use à vontade.
