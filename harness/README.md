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

### evaluate.sh
for repo in repos.csv:
    Host N+1 (Baseline, N Patches)      --> Calculate Baseline for the sake of consistency (because it's a new machine)
    Calculate CWV for N+1

git diff at the end.

- no if/else logic?
- check how the contract works in swe-bench etc. 
- how will the final eval / cwv scores be calculated?
- start with 1 repo... and make e2e loop


#### MAKE SURE TO RUN BEFORE EXECUTING EVALUATE.sh:
export AIDER_YES=true
export AIDER_NO_COLOR=true
export AIDER_NO_AUTO_COMMITS=true

2,anshumanjadiya1102/anshumanjadiya1102.github.io,04beabaea44311a728fa66238d5613102334b51b,REPO_SNAPSHOTS/anshumanjadiya1102__04beabaea44311a728fa66238d5613102334b51b.zip,host_files/host_static_html.sh,7516.0,0.02615457,33.3,237.3333,1.9667
