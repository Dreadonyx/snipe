#!/usr/bin/env bash
# Create/update the Snipe EC2 stack, then push the bot to it.
#     deploy/aws/deploy.sh            # stack name "snipe", region from your AWS CLI config
set -euo pipefail
cd "$(dirname "$0")/../.."
stack=${STACK_NAME:-snipe}
my_ip=$(curl -fsS https://checkip.amazonaws.com)

aws cloudformation deploy \
    --stack-name "$stack" \
    --template-file deploy/aws/snipe-ec2.yaml \
    --capabilities CAPABILITY_IAM \
    --no-fail-on-empty-changeset \
    --tags app=snipe \
    --parameter-overrides "AdminSshCidr=${my_ip}/32" "SshPublicKey=$(cat ~/.ssh/snipe_deploy.pub)"

ip=$(aws cloudformation describe-stacks --stack-name "$stack" \
    --query "Stacks[0].Outputs[?OutputKey=='PublicIp'].OutputValue" --output text)
echo "✅ Host ready at $ip"
deploy/push.sh "ubuntu@$ip"
