# Deploying RAGX for your class or team

This guide puts RAGX on the internet at your own address, for example `https://ragx.yourcollege.edu`, with HTTPS, nightly backups and accounts. It takes about 30 minutes. No programming is needed: you copy commands.

## What you need

| | Free option | Cheap option |
|---|---|---|
| **A server** (Linux, 2+ CPU, 4+ GB RAM, 20 GB disk) | Oracle Cloud "Always Free" Ampere VM (up to 4 CPU / 24 GB RAM). The sign-up asks for a card to verify identity, but the Always Free resources are not charged. | Hetzner, DigitalOcean or AWS Lightsail, about $5–10 per month |
| **A web address** that points to the server | A free subdomain from [DuckDNS](https://www.duckdns.org) (e.g. `myclass.duckdns.org`) | Your college or your own domain |
| **AI models** | A free Gemini key ([aistudio.google.com](https://aistudio.google.com)), or local Ollama models if the server has 16+ GB RAM | n/a |
| **Email** (optional, for "forgot password" and email confirmation) | Gmail with an app password, or the free tiers of Brevo or Resend | n/a |

## 1. Create the server

Create an **Ubuntu 24.04** server with your provider, and add your SSH key. Open ports **80** and **443** in the provider's firewall:
- **Oracle Cloud:** the "security list" of the network.
- **Hetzner / DigitalOcean:** "Firewall".

## 2. Point your address at the server

Create a DNS **A record** for your address (e.g. `ragx.yourcollege.edu`) with the server's public IP. With DuckDNS, type the IP on the DuckDNS page. Wait until this shows the IP:

```bash
nslookup ragx.yourcollege.edu
```

## 3. Install Docker and get RAGX

Connect to the server (`ssh ubuntu@YOUR-IP`), then run these one by one:

```bash
curl -fsSL https://get.docker.com | sudo sh
```

```bash
sudo usermod -aG docker $USER && newgrp docker
```

```bash
git clone https://github.com/dolliecoder/RAGX.git && cd RAGX
```

```bash
cp .env.example .env
```

On Oracle Cloud Ubuntu images, also allow web traffic in the server's own firewall:

```bash
sudo iptables -I INPUT -p tcp -m multiport --dports 80,443 -j ACCEPT && sudo netfilter-persistent save
```

## 4. Fill in `.env`

Open it with `nano .env`. Set at least these values, then save with `Ctrl+O`, `Enter`, `Ctrl+X`:

```
RAGX_DOMAIN=ragx.yourcollege.edu
RAGX_ADMIN_EMAILS=you@yourcollege.edu
POSTGRES_PASSWORD=<a long random password>
RAGX_GEMINI_API_KEY=<your free Gemini key>
```

Optional email, which enables "forgot password", email confirmation and invites. This example uses Gmail with an [app password](https://myaccount.google.com/apppasswords):

```
RAGX_SMTP_HOST=smtp.gmail.com
RAGX_SMTP_PORT=587
RAGX_SMTP_USER=you@gmail.com
RAGX_SMTP_PASSWORD=<16-character app password>
RAGX_SMTP_FROM=RAGX <you@gmail.com>
```

To get a strong database password:

```bash
openssl rand -base64 24
```

## 5. Start it

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

The first start takes a few minutes. Caddy gets a free HTTPS certificate automatically. Open `https://ragx.yourcollege.edu`:

1. **Sign up** with the email you put in `RAGX_ADMIN_EMAILS`. You become the administrator.
2. Go to **Users → Access & limits**:
   - set your college email domain
   - click **Generate** for a join code
   - optionally tick "Students must confirm their email" (needs email set up)
   - set a site-wide daily cap that fits your free model quota, e.g. 150 questions per day on the free Gemini tier
   - click **Save**, then **copy link**
3. Go to **Knowledge**, create a knowledge base, and **upload** your course documents.
4. Share the invite link with your students.

## Everyday operations

**Update to the latest version:**

```bash
cd RAGX && git pull && docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

Your data is kept, and new database columns are added automatically.

**Backups** are written every night to `RAGX/backups/` and kept for 14 days. Copy them somewhere else now and then. To restore one:

```bash
gunzip -c backups/ragx-2026-10-05-0300.sql.gz | docker compose exec -T db psql -U ragx -d ragx
```

**See what's happening:**

```bash
docker compose logs -f api
```

**Locked out of the admin account:**

```bash
docker compose exec api ragx admin reset-password you@yourcollege.edu
```

## Using local open-source models on the server

With 16+ GB RAM (for example the Oracle Always Free VM), you can run models on the server itself, with no API keys and no daily limits:

1. Start the bundled Ollama container:

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.prod.yml --profile ollama up -d
   ```

2. Download the models:

   ```bash
   docker compose exec ollama ollama pull qwen3:4b
   ```

   ```bash
   docker compose exec ollama ollama pull gemma3:4b
   ```

   ```bash
   docker compose exec ollama ollama pull nomic-embed-text
   ```

3. In `.env`, set `RAGX_OLLAMA_URL=http://ollama:11434` and use the setup-B `ollama:` model lines from `.env.example`. Restart with step 5's command.

On a CPU-only server expect about 30–90 seconds per answer. Mixing in free cloud models (setup C) makes it faster.

## Security checklist

- HTTPS on, via Caddy (automatic). Session cookies are marked secure in production.
- A strong `POSTGRES_PASSWORD`. The database is never exposed to the internet.
- A join code or an allowed email domain if sign-up is open.
- A site-wide daily cap so nobody can exhaust your free AI quota.
- Keep the server updated:

  ```bash
  sudo apt update && sudo apt upgrade
  ```
- Report security problems privately: see [SECURITY.md](SECURITY.md).
