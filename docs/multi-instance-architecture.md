# Multi-Instance Windrose Architecture

This document describes the safer target model for hosting more than one world without re-pointing a single running server at different save data.

## Goal

Run two isolated Windrose server instances:

- `Wayward Winds`
- `Waylaid Wanderers`

Only one of them should be running during an exclusive schedule window, but each instance keeps its own:

- `server-files`
- `.env`
- container name
- compose project name
- ports
- backup stream
- panel metadata

## Why This Is Safer

The previous single-instance world-rotation model changes `WorldIslandId` inside one live server install. That is lighter, but it increases the chance of:

- switching to the wrong world
- mixing operational state across worlds
- restarting the wrong runtime
- making recovery harder after operator error

Separate instances reduce the blast radius. The scheduler only decides which instance should be running. It does not repoint one server at a different world.

## Planned Layout

```text
/home/windrose/
  instances/
    wayward-winds/
      .env
      docker-compose.yml
      server-files/
    waylaid-wanderers/
      .env
      docker-compose.yml
      server-files/
  config/
    instances.json
```

## Transition Layout

The primary live server does not need to move immediately.

Transition-friendly approach:

- keep `Wayward Winds` on the current legacy paths for now
- represent it in config as a `legacy-bridge` instance
- add `Waylaid Wanderers` as a fully isolated instance
- later, during maintenance, move `Wayward Winds` into `instances/wayward-winds/`
- use symlinks during the cutover if that reduces risk

Example:

```text
/home/windrose/server-files -> still used by live Wayward Winds now
/home/windrose/instances/wayward-winds/server-files -> planned target later
```

The control plane should understand both layouts during the transition.

## Scheduler Model

Exclusive mode:

- outside scheduled windows: `Wayward Winds` runs
- inside the `Waylaid Wanderers` windows: `Wayward Winds` stops and `Waylaid Wanderers` starts

Important safety rule:

- the scheduler must act on instance IDs, never on raw world IDs

## Panel Model

The panel should move from one global server view to:

- instance cards
- per-instance start, stop, restart, update, backup
- per-instance logs and health
- schedule editor at the instance level

The current production panel can be refactored to this model without restarting the live game server. The code changes can be staged first and activated later during a test window.

## Migration Plan

1. Add tracked instance config schema and validation.
2. Add instance-aware helper scripts.
3. Support a `legacy-bridge` primary instance that still points at the current live paths.
4. Add panel support for multiple instances.
5. Create the second instance filesystem and env files without starting it.
6. Test `Waylaid Wanderers` on alternate ports.
7. Replace single-instance world rotation with instance scheduling.
8. Retire the old `WorldIslandId` scheduler path.

## Operational Rules

- Never move or rewrite the current `Wayward Winds` save data during scaffolding.
- Do not enable the second instance until its paths and ports are verified.
- Do not enable the scheduler against production until manual start and stop of both instances has been tested.
