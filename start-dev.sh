#!/bin/bash
# Start backend + frontend dev servers

echo "[backend] Starting Django on http://localhost:8000 ..."
source backend/venv/Scripts/activate && python backend/manage.py runserver &
BACKEND_PID=$!

echo "[frontend] Starting Vite on http://localhost:5173 ..."
cd frontend && npm run dev &
FRONTEND_PID=$!

echo ""
echo "Both servers running. Press Ctrl+C to stop."
wait $BACKEND_PID $FRONTEND_PID
