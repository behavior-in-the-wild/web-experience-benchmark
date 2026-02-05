# Agent-Environment: Contract
## Agent
- Agent will have linux env (docker setup)
- Agent will have access to the codebase (local snapshot version)

## Environment
1. (Local Snapshot of Repo, Host.sh) --> Instructions to locally deploy the repository on a target localhost port
2. For next step, docker container will be used to deploy the repository on a target localhost port
3. MCP (Calculate CWV)

## Dataset
Our Dataset ID, Github Username/Reponame, Commit ID, ZIPped Repository Path, (host.sh), Desktop LCP, Desktop CLS, Desktop FID, Desktop INP, Desktop TTFB, Mobile LCP, Mobile CLS, Mobile FID, Mobile INP, Mobile TTFB

## Output:
ID, N Patch File(s)

#### MAKE SURE TO RUN BEFORE EXECUTING EVALUATE.sh:
export AIDER_YES=true
export AIDER_NO_COLOR=true
export AIDER_NO_AUTO_COMMITS=true


$ sudo apt-get update -qq && sudo apt-get install -y zip