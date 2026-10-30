# Claude agent

Fait tourner Claude Code sur bfiven, piloté depuis l'appli Claude par Remote Control, avec un Google Chrome
graphique (extension « Claude in Chrome ») qui ne tourne qu'à la demande. Tout tourne sous l'utilisateur `claude`,
sans Docker : le native messaging de l'extension impose le même utilisateur que Claude Code, et le sandbox de Chrome
s'accommode mal d'un conteneur. L'isolation vient de systemd (voir « Confinement »).

Projets : un dossier par projet dans `/home/claude/projects/` (déclarés dans `claude_agent_projects`,
`host_vars/bfiven/claude_agent.yml`), chacun avec son propre Remote Control.

```bash
ansible-playbook -i inventories/prod.yml run_claude_agent.yml -l bfiven
# Nouveau projet : le déclarer dans claude_agent_projects, puis le copier une fois (jamais écrasé ensuite)
ansible-playbook -i inventories/prod.yml run_claude_agent.yml -l bfiven \
  -e '{"claude_agent_project_sources": {"coursator": "/mnt/c/Users/fiorentinob/Downloads/coursator/"}}'
```

## Briques

| Unité systemd | Rôle | Démarrage |
|---|---|---|
| `claude-firewall` | Table nftables `inet claude_agent` : VNC seulement depuis la boucle locale, noVNC depuis la boucle locale et le téléphone / PC du VPN, port DevTools de Chrome réservé aux processus de `claude`. N'ouvre aucun port. | boot |
| `claude-xvfb` | Écran virtuel `:1` en 1600x1000 | boot |
| `claude-openbox` | Gestionnaire de fenêtres (menu clic droit : démarrer / arrêter Chrome) | boot |
| `claude-x11vnc` | Serveur VNC sur `127.0.0.1:5901` | boot |
| `claude-novnc` | noVNC (websockify) sur `10.100.0.2:6080`, adresse WireGuard de bfiven | boot |
| `claude-chrome.path` | Surveille la demande de Chrome | boot |
| `claude-chrome` | Google Chrome, profil `~claude/.config/claude-chrome`, DevTools sur `127.0.0.1:9222` | **à la demande**, arrêt seul après 4 h |
| `claude-tmux` | Session tmux `claude`, une fenêtre par projet | boot |
| `coursator-web` | Interface web de Coursator (`web/server.py`) sur `127.0.0.1:8765` | boot |
| `coursator-web-restart.path` | Redémarre `coursator-web` quand `claude-web coursator-web restart` le demande (seule unit que `claude` peut redémarrer) | boot |
| `coursator-web-proxy.socket` | `172.30.99.1:8765`, réservé à Traefik (réseau Docker `host-services`), relayé vers `127.0.0.1:8765` | boot |
| `pas-web` (+ `-restart.path`, `-proxy.socket`) | Grille d'observation du PAS : fichiers statiques de `web/` sur `127.0.0.1:8766` | boot |

## Chrome à la demande

```bash
claude-browser start    # démarre Chrome et attend que l'extension soit connectée (30 s au plus)
claude-browser stop
claude-browser status
```

Claude Code le sait par `~claude/.claude/CLAUDE.md` (bloc géré par Ansible) et a le droit de lancer ces commandes
sans demander (`/etc/claude-code/managed-settings.json`). Dans noVNC, clic droit sur le bureau : « Démarrer Chrome »
/ « Arrêter Chrome ». Chrome s'arrête de lui-même après 4 h ; il redémarre seul s'il plante.

## Port de debug de Chrome

Chrome 136+ ignore `--remote-debugging-port` sur son répertoire par défaut : le profil est dans
`~claude/.config/claude-chrome` (copie unique de `~claude/.config/google-chrome`, gardé en sauvegarde ; son
`NativeMessagingHosts`, où Claude Code écrit le manifeste de l'extension, est partagé par un lien). Le port
`127.0.0.1:9222` (`claude_agent_chrome_debugging_port`) donne le contrôle complet du Chrome connecté : il n'écoute
que sur la boucle locale, n'est jamais publié, et `claude-firewall` refuse toute connexion qui ne vient pas d'un
processus de `claude` (qui pilote déjà ce Chrome par l'extension). Le vider dans `host_vars` le désactive.

## Playwright

Playwright pour Python (`claude_agent_playwright_version`), installé par root dans `/opt/playwright` (virtualenv
`venv/`, navigateurs `browsers/` avec leurs dépendances apt) : `claude` s'en sert sans pouvoir le modifier.
`import playwright` marche avec son `python` (un `.pth` dans son site-packages utilisateur), `playwright` est dans son
PATH, et `PLAYWRIGHT_BROWSERS_PATH` est posé dans `~/.profile` et dans les units tmux et web. Pour un autre navigateur
(`firefox`, `webkit`) : l'ajouter à `claude_agent_playwright_browsers` et repasser le playbook ; `playwright install`
échoue sous `claude`.

Son Chromium part d'un profil vierge : Playwright ne passe pas par `claude-guard`, il n'a donc pas à toucher au
Chrome connecté (consigne dans `~claude/.claude/CLAUDE.md`).

## RTK

[RTK](https://github.com/rtk-ai/rtk) (`claude_agent_rtk_version`) compacte la sortie des commandes shell avant
qu'elle n'entre dans le contexte : un hook `PreToolUse` sur `Bash`, déclaré dans `/etc/claude-code/managed-settings.json`,
réécrit `git status` en `rtk git status`. Binaire installé par root dans `/opt/rtk/<version>/` (archive vérifiée par
sa somme SHA-256), lien `/usr/local/bin/rtk`. Pas de `rtk init` : il écrirait le hook dans `~/.claude/settings.json`,
que l'agent peut modifier ; `RTK_SUPPRESS_HOOK_WARNING=1` (même fichier) fait taire son conseil de le lancer. La
consigne (`rtk proxy <commande>` pour la sortie brute) est dans `~claude/.claude/CLAUDE.md`.

Le hook ne valide lui-même une commande réécrite que si une règle `allow` des réglages utilisateur ou projet la couvre ;
sinon la vérification habituelle de Claude Code s'applique, comme sans RTK. La télémétrie de RTK reste coupée (elle
demande un consentement explicite), les filtres TOML d'un projet aussi tant qu'on ne les a pas approuvés
(`rtk trust`). Suivi des gains : `sudo -u claude /usr/local/bin/rtk gain` (base dans `~claude/.local/share/rtk`).

Mise à jour : `claude_agent_rtk_version` et `claude_agent_rtk_checksum` (somme de
`rtk-x86_64-unknown-linux-musl.tar.gz` sur la release GitHub), puis repasser le playbook. Le lien bascule aussitôt,
sessions ouvertes comprises ; `/opt/rtk/<ancienne version>` reste, à supprimer à la main.

## Interfaces web

`claude_agent_web_services` (`host_vars/bfiven/claude_agent.yml`) : un service par interface, sous `claude`, avec le
même confinement que la session tmux (il lance des `claude -p`). Traefik la publie en HTTPS, réservée au VPN
(`claude_agent_web_allowed_source_ranges`, plus, par service, les adresses publiques fixes de son
`extra_allowed_source_ranges`) et derrière une basic auth (`claude_agent_web_users`). Traefik la joint
par une socket sur la passerelle du réseau Docker `host-services` qui n'accepte que ce réseau (seul Traefik y est
attaché), relayée par `systemd-socket-proxyd` vers `127.0.0.1` : le service garde `IPAddressAllow=localhost`.

- Coursator : <https://courses.moi.ovh/>, VPN actif (blocky la résout en `10.100.0.2` ; un appareil qui n'utilise pas
  blocky comme DNS reçoit un 403 : l'ajouter à son fichier hosts). Aussi sans VPN depuis les adresses de son
  `extra_allowed_source_ranges` (DNS public, `188.165.251.28`).
- PAS : <https://pas.moi.ovh/>, VPN seul. Application statique (`python3 -m http.server` sur le dossier `web/` du
  projet) : les données des élèves restent dans le navigateur, jamais sur le serveur.
- Mots de passe, générés sur le serveur : `sudo cat /etc/claude-agent/web-users.json`. Pour en changer un : retirer
  son entrée du fichier et repasser le playbook.
- `claude` (donc Claude Code, depuis la session tmux) redémarre son interface sans privilège : `claude-web <service>
  restart` écrit une demande dans `~claude/.local/state/claude-web/`, qu'une unit `.path` de root surveille pour
  redémarrer ce service et lui seul. Pas de sudo (le confinement l'interdit de toute façon), ni de règle polkit
  (la 0.105 d'Ubuntu 22.04 ne sait pas limiter à une unit).
- Ordre au premier déploiement : `run_traefik.yml` (réseau `host-services`, provider fichier), puis
  `run_claude_agent.yml`, puis `run_blocky.yml -l bfivenus`.

```bash
claude-web coursator-web restart              # après une mise à jour de web/server.py, aussi en tant que claude
journalctl -u coursator-web -f                 # et -u coursator-web-proxy
sudo -u claude tail -f ~claude/projects/coursator/web/logs/$(date +%F).jsonl
```

## Joindre l'écran

- **Téléphone ou PC** : activer WireGuard, puis ouvrir <http://10.100.0.2:6080/>. Le téléphone passe par bfivenus,
  qui relaie vers bfiven dans le tunnel. Seuls `phone` (10.100.0.10) et `pc` (10.100.0.11) sont acceptés.
- **Sans VPN, par SSH** : `ssh -L 6080:10.100.0.2:6080 bfiven` puis <http://localhost:6080/>.
- Mot de passe VNC, généré sur le serveur : `sudo cat /home/claude/.vnc/password`.
  Pour le changer : supprimer `/home/claude/.vnc/passwd` et relancer le playbook.

## Remote Control

```bash
claude-remote-control coursator start     # `claude remote-control --chrome` dans la fenêtre tmux claude:coursator
claude-remote-control coursator restart   # après une mise à jour de Claude Code (prise en compte au redémarrage)
claude-remote-control coursator stop
claude-remote-control coursator status
```

Pour le lancer au boot : `claude_agent_remote_control_autostart: ["coursator"]` dans `host_vars/bfiven/claude_agent.yml`,
repasser le playbook puis `sudo systemctl restart claude-tmux`.

## Confinement

La session tmux (donc Claude Code et toutes les commandes qu'il lance) et Chrome tournent confinés par systemd :

- **Fichiers** : `/data`, `/root`, `/etc/restic`, `/srv`, `/var/backups`, `/var/lib/docker` et les autres homes
  sont invisibles ; le système est en lecture seule ; écriture seulement dans `/home/claude` et `/tmp`.
- **Réseau** : aucune adresse privée (conteneurs Docker, réseau local, VPN, métadonnées cloud) ; Internet reste ouvert.
- **Privilèges** : pas de sudo ni de binaire setuid, pas de module ni de réglage du noyau.

Le serveur graphique (Xvfb, openbox, x11vnc) et les interfaces web le sont aussi : tout ce qui tourne sous `claude`
est confiné, sauf ce qu'un administrateur lance lui-même. Or le dossier de `claude` est écrit par l'agent
(`~/.local/bin`, `~/.profile`, `~/.bashrc`…) : n'y rien exécuter hors confinement.

- **Jamais `sudo -iu claude`** : le shell de connexion lit `~/.profile` et `~/.bashrc`, que l'agent peut avoir
  modifiés, et ce qu'il lance n'est pas confiné. Pour rejoindre la session :
  `sudo -u claude env -i HOME=/home/claude TERM="$TERM" /usr/bin/tmux attach -t claude`.
- `claude-health`, `claude-remote-control`, `claude-browser` et `claude-web` n'exécutent que des commandes du système.

## Garde-fous de Claude Code

Communs à toutes les sessions, hors de portée de l'agent (fichiers de root, au-dessus des réglages utilisateur et
projet) : `/etc/claude-code/managed-settings.json` et le hook `claude-guard` (`files/claude-guard`, configuré dans
`/etc/claude-agent/claude-guard.json`), appelé avant chaque outil du navigateur ou de lecture de fichier. Le hook RTK
(voir plus haut) y est déclaré aussi, pour les commandes shell.

- **Toutes les sessions** : le navigateur n'ouvre que `claude_agent_browser_allowed_domains` et jamais une page ou un
  appel de `claude_agent_browser_forbidden_patterns` (créneaux, vidage du panier, paiement, compte) ; le JavaScript
  exécuté dans les pages ne cite aucun autre domaine ; `claude_agent_secret_paths` (connexion claude.ai, profils
  Chrome, clés) n'est jamais lu ; pas de `--dangerously-skip-permissions`.
- **Sessions sans surveillance** (les `claude -p` d'une interface web, reconnues à leur unité systemd) : en plus,
  seulement les fichiers du projet, pas de clic / saisie / envoi de fichier dans le navigateur ni d'accès web hors
  navigateur, et le `localStorage` des pages n'est réécrit qu'avec les helpers du projet, collés tels quels.

C'est un filet, pas une garantie : du JavaScript obfusqué peut joindre un autre domaine. Le seul rempart qui ne
dépend pas du modèle reste l'absence de carte bancaire enregistrée sur le compte. Un appel refusé l'est avec son
motif, que Claude voit ; le hook refuse aussi en cas d'erreur interne.

## État et redémarrages

```bash
claude-health                                  # état de chaque brique (code retour = nombre d'échecs)
sudo systemctl restart claude-xvfb             # redémarre aussi openbox, VNC et Chrome
sudo systemctl restart claude-novnc            # ou claude-x11vnc, claude-openbox, claude-firewall
sudo systemctl restart claude-tmux             # attention : tue les sessions Claude Code en cours
sudo -u claude env -i HOME=/home/claude TERM="$TERM" /usr/bin/tmux attach -t claude
```

## Journaux

```bash
journalctl -u claude-chrome -f                 # idem pour chaque unité claude-*
journalctl -u 'claude-*' --since today
```

Claude Code : la fenêtre tmux du projet (elle reste ouverte quand la commande s'arrête), et `~claude/.claude/`.
Sauvegarde de `projects/coursator/data` : `/var/log/restic_backup.log`.

## Ce que le rôle ne fait pas

Aucun identifiant (sites, claude.ai) n'est stocké par Ansible : connexions à la main, une fois, depuis noVNC et
la session tmux.
