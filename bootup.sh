#!/bin/bash

ansible-playbook -i inventory.prod host_bootup.yml -l bfiven
