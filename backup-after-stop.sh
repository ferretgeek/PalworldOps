#!/bin/sh
set -eu
exec /opt/palworld/bin/palworldctl backup create --kind event --if-changed --nonblocking
