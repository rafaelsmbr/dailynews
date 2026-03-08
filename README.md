# Wired RSS Proxy

Flask app que lê o feed RSS da Wired (tag AI) e enriquece cada item com o conteúdo completo do artigo, expondo um endpoint XML pronto para consumo.

## Endpoints

| Rota | Descrição |
|------|-----------|
| `GET /rss` | Feed RSS enriquecido com `<fullContent>` |
| `GET /health` | Healthcheck (`{"status": "ok"}`) |

---

## Deploy no Render

### Pré-requisitos
- Conta em [render.com](https://render.com)
- Repositório no GitHub/GitLab com estes arquivos

### Passo a passo

1. **Suba os arquivos para um repositório Git:**
   ```bash
   git init
   git add .
   git commit -m "initial commit"
   git remote add origin https://github.com/SEU_USER/SEU_REPO.git
   git push -u origin main
   ```

2. **No dashboard do Render:**
   - Clique em **New → Web Service**
   - Conecte seu repositório GitHub/GitLab
   - O Render detecta automaticamente o `render.yaml` e preenche as configurações

3. **Ou use o Blueprint (render.yaml):**
   - Vá em **New → Blueprint**
   - Selecione o repositório — o Render criará o serviço automaticamente com as configs do `render.yaml`

4. Clique em **Deploy** e aguarde o build (~2 min)

5. Acesse seu feed em:
   ```
   https://SEU-SERVICO.onrender.com/rss
   ```

---

## Rodar localmente

```bash
pip install -r requirements.txt
python wired_rss_proxy.py
# acesse http://localhost:5000/rss
```

## Estrutura dos arquivos

```
.
├── wired_rss_proxy.py   # Aplicação Flask principal
├── requirements.txt     # Dependências Python
├── render.yaml          # Configuração de deploy no Render
├── .gitignore
└── README.md
```
