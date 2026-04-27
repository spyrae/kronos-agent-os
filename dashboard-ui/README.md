# KAOS Dashboard UI

React control room for Kronos Agent OS. The dashboard surfaces runtime state,
memory, jobs, tool calls, skills, sub-agent coordination status, and capability
gates exposed by the FastAPI backend in `../dashboard`.

## Requirements

- Node.js `>=18.18.0`
- npm `>=10`

Use the repository `.nvmrc` when working locally:

```bash
nvm use
```

## Commands

```bash
npm install
npm run dev
npm run build
```

The Vite dev server expects the dashboard API to be running separately.
