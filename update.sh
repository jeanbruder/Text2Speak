#!/bin/bash
DC_ACTIONS=('stop' 'rm -f' 'build' 'up -d')

# docker build --compress .

for action in "${DC_ACTIONS[@]}"
do
	echo "Docker-compose action : '$action' ..."
        sudo docker-compose $action
done
echo "Done ..."

# EoF
