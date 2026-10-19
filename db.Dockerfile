FROM postgres:16

# The postgres image runs these once, in name order, when its data volume is empty.
# Copied in rather than mounted, so Docker never needs access to the project folder.
COPY migrations/ /docker-entrypoint-initdb.d/
