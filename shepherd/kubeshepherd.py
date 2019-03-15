from shepherd.shepherd import Shepherd
from shepherd.shepherd import FlockRequest

import traceback
import json
import os
import time


class KubeShepherd(Shepherd):
    DEF_SEC_CONTEXT = {
      'capabilities': {'add': ['NET_ADMIN', 'SYS_ADMIN', 'SETUID']},
    }

    DEF_VOLUMES = {
        'name': 'dshm',
        'emptyDir': {'medium': 'Memory',
                    'sizeLimit': '1024Mi'}
    }

    DEF_VOL_BINDS = {
        'mountPath': '/dev/shm',
        'name': 'dshm'
    }

    def __init__(self, *args, **kwargs):
        super(KubeShepherd, self).__init__(*args, **kwargs)

        self.job_duration = kwargs.get('job_duration')

        from kubernetes import client, config

        if os.environ.get('IN_CLUSTER'):
            config.load_incluster_config()
        else:
            config.load_kube_config()

        configuration = client.Configuration()
        api_client = client.ApiClient(configuration)

        self.batch_api = client.BatchV1Api(api_client)
        self.core_api = client.CoreV1Api(api_client)

    def get_volumes(self, flock_req, flock_spec, labels=None, create=False):
        volume_spec = flock_spec.get('volumes')
        if not volume_spec:
            return None, None

        volumes_list = []
        volume_binds = []

        for n, v in volume_spec.items():
            volumes_list.append({
                'name': n,
                'emptyDir': {}
            })

            volume_binds.append({
                'name': n,
                'mountPath': v
            })

        volume_binds.append(self.DEF_VOL_BINDS)
        volumes_list.append(self.DEF_VOLUMES)

        return volume_binds, volumes_list

    def start_flock(self, reqid,
                    labels=None,
                    environ=None,
                    pausable=False,
                    network_pool=None):

        flock_req = FlockRequest(reqid)
        if not flock_req.load(self.redis):
            return {'error': 'invalid_reqid'}

        response = flock_req.get_cached_response()
        if response:
            return response

        flock_req.update_env(environ)

        try:
            flock_name = flock_req.data['flock']
            image_list = flock_req.data['image_list']
            flock_spec = self.flocks[flock_name]
        except:
            return {'error': 'invalid_flock',
                    'flock': flock_name}

        labels = labels or {}
        labels[self.reqid_label] = flock_req.reqid

        try:
            volume_binds, volumes = self.get_volumes(flock_req, flock_spec, labels)

            containers = []
            ports = []

            container_infos = {}

            req_deferred = flock_req.data.get('deferred', {})

            for image, spec in zip(image_list, flock_spec['containers']):
                name = spec['name']

                if spec.get('deferred'):
                    continue

                environ = spec.get('environment') or {}
                if 'environ' in flock_req.data:
                    environ = environ.copy()
                    environ.update(flock_req.data['environ'])

                env_list = []
                for n, v in environ.items():
                    env_list.append({'name': n, 'value': v})

                port_info = {}

                for port_name, port in spec.get('ports', {}).items():
                    if isinstance(port, int) or '/' not in port:
                        protocol = 'TCP'
                    else:
                        port, protocol = port.split('/', 1)
                        protocol = protocol.upper()

                    ports.append({
                        'name': port_name,
                        'port': int(port),
                        'protocol': protocol
                    })

                    port_info[port_name] = ''

                containers.append({
                    'name': name,
                    'image': image,
                    'imagePullPolicy': 'IfNotPresent',
                    'volumeMounts': volume_binds,
                    'env': env_list,
                    'securityContext': self.DEF_SEC_CONTEXT,
                })

                container_infos[name] = {
                    'environ': environ,
                    'ports': port_info,
                    'id': name
                }

            container_spec = {'containers': containers,
                              'volumes': volumes,
                              'restartPolicy': 'Never'
                             }


            metadata = {'labels': labels}

            job = {
                'apiVersion': 'batch/v1',
                'kind': 'Job',
                'metadata': {'name': 'flock-' + flock_req.reqid.lower()},
                'spec': {
                    'template': {'metadata': metadata,
                                 'spec': container_spec}
                }
            }

            if self.job_duration:
                job['spec']['activeDeadlineSeconds'] = int(self.job_duration)

            res = self.batch_api.create_namespaced_job(body=job, namespace='default')
            #pprint(res)

            service = {
                'kind': 'Service',
                'apiVersion': 'v1',
                'metadata': {'name': 'service-' + flock_req.reqid.lower(),
                             self.reqid_label: flock_req.reqid},
                'spec': {
                    'type': 'NodePort',
                    'selector': {self.reqid_label: flock_req.reqid},
                    'ports': ports
                }
            }

            res = self.core_api.create_namespaced_service(body=service, namespace='default')
            #pprint(res)

            # fill in ports
            cluster_ip = res.spec.cluster_ip

            port_res = {}
            for port_val in res.spec.ports:
                port_res[port_val.name] = port_val.node_port

            for info in container_infos.values():
                info['ip'] = cluster_ip
                for name in info['ports']:
                    info['ports'][name] = port_res.get(name)

            return {'containers': container_infos,
                    'network': '',
                   }

        except:
            traceback.print_exc()

    def stop_flock(self, reqid, keep_reqid=False, grace_time=None, network_pool=None):
        flock_req = FlockRequest(reqid)
        if not flock_req.load(self.redis):
            return {'error': 'invalid_reqid'}

        res = self.batch_api.delete_namespaced_job(
            #label_selector=self.reqid_label + '=' + flock_req.reqid,
            name='flock-' + flock_req.reqid.lower(),
            namespace='default',
            body={'gracePeriodSeconds': grace_time or 0, 'propagationPolicy': 'Foreground'})

        #pprint(res)

        res = self.core_api.delete_namespaced_service(
            name='service-' + flock_req.reqid.lower(),
            namespace='default',
            body={'gracePeriodSeconds': grace_time or 0, 'propagationPolicy': 'Foreground'})

        #pprint(res)

        # delete flock after docker removal is finished to avoid race condition
        # with 'untracked' container removal
        if not keep_reqid:
            flock_req.delete(self.redis)
        else:
            flock_req.stop(self.redis)

        return {'success': True}

    def pause_flock(self, reqid, grace_time=1):
        flock_req = FlockRequest(reqid)
        if not flock_req.load(self.redis):
            return {'error': 'invalid_reqid'}

        #TODO scale to 0
        return {'error': 'not_supported'}

    def resume_flock(self, reqid):
        flock_req = FlockRequest(reqid)
        if not flock_req.load(self.redis):
            return {'error': 'invalid_reqid'}

        state = flock_req.get_state()
        if state != 'paused':
            return {'error': 'not_paused', 'state': state}

        try:
            # TODO: scale to 1
            raise Exception('not_implemented')

            flock_req.set_state('running', self.redis)

        except:
            traceback.print_exc()

            return {'error': 'resume_failed',
                    'details': traceback.format_exc()
                   }

        return {'success': True}

