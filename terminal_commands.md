# terminal commands

`bash -lc '
cd /home/ssm-user/working/arnav/cwv-agent-main/cwv-agent &&
. .venv/bin/activate &&
export CWV_AGENT_NO_SANDBOX=1 &&
export AIDER_IGNORE="fonts/**,*.woff,*.woff2,*.ttf,*.eot,*.otf" &&
[ -x /usr/bin/google-chrome ] && export PUPPETEER_EXECUTABLE_PATH=/usr/bin/google-chrome || true &&
set -a &&
. .env &&
set +a &&
cwv-optimizer framework \
  -g https://github.com/adchs/adchs.github.io \
  -f "Static HTML" \
  --model azure/gpt-4.1
'
`


`bash -lc '
cd /home/ssm-user/working/arnav/cwv-agent-main/cwv-agent &&
. .venv/bin/activate &&
export CWV_AGENT_NO_SANDBOX=1 &&
export AIDER_IGNORE="fonts/**,*.woff,*.woff2,*.ttf,*.eot,*.otf" &&
[ -x /usr/bin/google-chrome ] && export PUPPETEER_EXECUTABLE_PATH=/usr/bin/google-chrome || true &&
set -a &&
. .env &&
set +a &&
cwv-optimizer framework \
  -g https://github.com/adchs/adchs.github.io \
  -f "Static HTML"
'
`