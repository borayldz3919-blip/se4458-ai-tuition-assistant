#!/bin/sh
# Runs every unit-test suite. No cloud, gateway or LLM needed.
#   docker compose --profile test run --rm tests
#   (or, with Python installed: sh scripts/run_tests.sh)
export PYTHONDONTWRITEBYTECODE=1
pip install -q --root-user-action=ignore -r agent/requirements-dev.txt 2>/dev/null || pip install -q -r agent/requirements-dev.txt
status=0
if [ -n "$TEST_DATABASE_URL" ]; then
  echo "API tests will use PostgreSQL: ${TEST_DATABASE_URL%%@*}@***"
else
  echo "TEST_DATABASE_URL not set: API tests use in-memory SQLite (set it to use PostgreSQL)"
fi
for d in tuition_api mcp_server agent gateway; do
  echo
  echo "================================================== $d"
  (cd "$d" && python -m pytest -v -p no:cacheprovider) || status=1
done
echo
[ "$status" -eq 0 ] && echo "ALL TEST SUITES PASSED" || echo "SOME TESTS FAILED"
exit $status
