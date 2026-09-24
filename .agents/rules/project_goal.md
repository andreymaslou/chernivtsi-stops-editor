---
description: "Global context regarding the ownership, ultimate goal, and target platforms of the project."
---

# Project Context & Ultimate Goal

**Context:** The user owns the official website `trans-gps.cv.ua` and the mobile applications **"TransGPS: Транспорт Чернівці"** (available on App Store and Google Play).

**The Ultimate Goal:** All the work currently being done in the `chernivtsi-stops-editor` (including the Python backend, `graph.json`, routing algorithms, Slang Panel, AI assistant logic, and the Emulator UI) is a testing ground and prototyping phase.
**The final destination of this logic is to be integrated/ported into the React Native mobile applications** (developed by the user's friend).

### Rules for AI Agents
1. **Architecture Decisions:** Always keep in mind that the UI code here (HTML/Vanilla JS) is temporary/prototyping. The core value lies in the data structures (`graph.json`, `stops.json`), the routing algorithms (`router_layer.py`), and the API responses (`main.py`), which will be consumed by or ported to React Native.
2. **API Design:** Ensure the Python API (`api_router`) is robust, clean, and easily consumable by mobile clients (JSON payloads, clear error handling).
3. **Collaboration:** The user is the owner and visionary. When suggesting features or refactoring, consider how easily it will translate to a React Native mobile environment.
