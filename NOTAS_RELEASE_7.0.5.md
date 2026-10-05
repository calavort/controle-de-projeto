# Controle de Projeto 7.0.5

- **Progresso de detalhamento: itens sem posição decimal agora são reconhecidos.** Em projetos como a montagem de escadas, as peças não têm `9.1`, `9.2`: o item aparece só como o balão `9`, `10`, `11`. O painel só lia decimais e mostrava 1 de 17 com tudo detalhado.
- A regra vale **somente** para o item que não tem peça decimal no modelo. Item com decimais continua sendo reconhecido só pela marca decimal, e balão inteiro dele continua ignorado, como antes.
- Fora da contagem: vista de detalhe (DetailView), rótulo solto sem peça (como o número do eixo) e vista geral com balões de quase todos os itens (o 3D da folha da lista de material).
- Os itens já detalhados que o balão passa a achar entram como histórico, sem inventar tempo nem abrir sessão "Em detalhamento" para todos de uma vez. O que for detalhado depois entra normalmente.
- Aba de cota: item de posição única deixa de ser cobrado como "sem subitem".
- Mede por conjunto e por componente como antes; o modo por componente não usa o balão.
