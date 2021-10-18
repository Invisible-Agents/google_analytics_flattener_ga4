from dm_helper import GaFlattenerDeploymentConfiguration


def GenerateConfig(ctx):
    config = GaFlattenerDeploymentConfiguration(ctx.env)

    resources = {
        'resources': [{
            'name': 'topic-name',
            'type': 'pubsub.v1.topic',
            'properties': {
                'topic': config.get_topic_name()
            },
            'accessControl':
                {'gcpIamPolicy':
                    {'bindings': [{
                        'role': 'roles/pubsub.publisher',
                        'members': ["serviceAccount:cloud-logs@system.gserviceaccount.com"]
                    }]}
                }
        },
            {
                'name': 'topic-name-intraday',
                'type': 'pubsub.v1.topic',
                'properties': {
                    'topic': config.get_topic_name(intraday=True)
                },
                'accessControl':
                    {'gcpIamPolicy':
                        {'bindings': [{
                            'role': 'roles/pubsub.publisher',
                            'members': ["serviceAccount:cloud-logs@system.gserviceaccount.com"]
                        }]}
                    }
            }
        ]
    }
    return resources
