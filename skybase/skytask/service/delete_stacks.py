import logging

import skybase.skytask
import skybase.exceptions
import skybase.actions.skycloud
import skybase.actions.skychef
from skybase.planet import Planet
from skybase.skytask import SkyTask
from skybase.utils.logger import Logger
from skybase import skytask
from skybase.service import SkyRuntime
from skybase.actions.dbstate import PlanetStateDbQuery
from skybase.utils import simple_error_format
from skybase.actions.skycloud import list_objects
from skybase.service.state import ServiceRegistryRecord

def service_delete_stacks_add_arguments(parser):
    parser.add_argument(
        '-p', '--planet',
        dest='planet_name',
        action='store',
        required=True,
        help='planet name')

    parser.add_argument(
        '-s', '--service',
        dest='service_name',
        action='store',
        required=True,
        help='service name.')

    parser.add_argument(
        '-t', '--tag',
        dest='tag',
        action='store',
        required=True,
        help='deployment tag or continent.')

    parser.add_argument(
        '-k', '--stack-name',
        dest='stack_name',
        action='store',
        help='single stack name.')

    parser.add_argument(
        '--delete-all-stacks',
        dest='delete_all_stacks',
        action='store_true',
        default=False,
        help='delete all stacks in service')

    parser.add_argument(
        '-y', '--assumeyes',
        dest='assumeyes',
        action='store_true',
        default=False,
        help='answer yes for all questions')

    parser.add_argument(
        '-m', '--mode',
        dest='exec_mode',
        action='store',
        choices={'local', 'restapi'},
        default='restapi',
        help='execution mode (default REST api)'
    )


class DeleteStacks(SkyTask):
    def __init__(self, all_args=None, runner_cfg=None):
        SkyTask.__init__(self, all_args, runner_cfg)
        self.logger = Logger(logging.getLogger(__name__), logging.INFO)
        self.name = 'service.delete_stacks'
        self.args = all_args
        self.runner_cfg = runner_cfg
        self.planet = None

        # create runtime object with command options
        self.runtime = SkyRuntime(
            tag=all_args.get('tag'),
            apply=all_args.get('apply', False))

        # initialize stack deletion process drivers
        self.stack_deletion_list = []
        self.stack_deletion_info = dict()

    def preflight_check(self):
        # container for preflight check issues
        preflight_result = []

        # instantiate planet
        try:
            self.planet = Planet(self.args.get('planet_name'))
        except Exception as e:
            self.preflight_check_result.status = 'FAIL'
            preflight_result.append(skybase.exceptions.SkyBaseValidationError('planet init: {0}'.format(simple_error_format(e))))

        # validate required options to delete one or all stacks
        if not (self.args.get('stack_name') or self.args.get('delete_all_stacks'))\
           or (self.args.get('stack_name') and self.args.get('delete_all_stacks')):
            self.preflight_check_result.status = 'FAIL'
            preflight_result.append(('specify one stack or option to delete all stacks for {0}'.format(self.args.get('service_name'))))

        # validate existence of requested service/stack in state registry
        query = PlanetStateDbQuery(
            planet=self.args.get('planet_name'),
            service=self.args.get('service_name'),
            tag=self.runtime.tag,
            stack=self.args.get('stack_name'),
            query_type='exact',
        )

        # verify unique pointer to service or service/stack
        if not query.can_find_exact():
            self.preflight_check_result.status = 'FAIL'
            preflight_result.append((skybase.exceptions.SkyBaseDeployError(
                'options do not identify a unique service or stack: {0}'.format(query.show_query_path())
            )))

        # TODO: push stack path into task result
        # save validated query path for postprocessing
        self.stack_path = query.query

        # reconfigure query to read resources for one or many service stacks
        query.query_type = query.WILDCARD
        query.query = query.make_query()

        # collect all stacks for deletion
        result_set = query.execute()

        # collect status of launched stacks
        for record in result_set:
            # accumulate list of provider stack ids for deletion
            self.stack_deletion_list.append(record.cloud.stack_id)

            try:
                # verify cloud provider DELETE* status for stack id
                stack_status = skybase.actions.skycloud.call_cloud_api(
                    planet=self.planet,
                    stack_name=record.cloud.stack_id,
                    action='get_stack_status')
            except Exception as e:
                raise skybase.exceptions.SkyBaseDeployError(skybase.utils.simple_error_format(e))

            if stack_status.startswith('DELETE'):
                self.preflight_check_result.status = 'FAIL'
                preflight_result.append(skybase.exceptions.SkyBaseDeployError(
                    'cannot delete stack {0} with status {1}'.format(record.cloud.stack_id, stack_status)))

            # accumulate stack information for deleting state db records and logging result
            self.stack_deletion_info[record.cloud.id] = {
                'stack_id': record.cloud.stack_id,
                'stack_name': record.cloud.stack_name,
            }

        # determine if deployed service used chef server. if so, then prepare to delete chef nodes

        # TODO: find authoritative location/source for skybase id definition
        # skybase state DB id
        skybase_id = '/{0}/{1}/{2}'.format(
            self.args.get('planet_name'),
            self.args.get('service_name'),
            self.runtime.tag,
        )

        # init service registry record and examine blueprint chef type
        service_record = ServiceRegistryRecord.init_from_id(skybase_id)
        self.chef_type = service_record.blueprint.definition.get('chef_type')
        self.is_chef_type_server = (self.chef_type and self.chef_type == 'server')

        # prepopulate list of host/instance names for use in chef node delete when chef_type server
        # NOTE:
        self.stack_chef_nodes = dict()

        if self.is_chef_type_server:

            for skybase_stack_id, stack_info in self.stack_deletion_info.items():

                self.stack_chef_nodes[stack_info['stack_name']] = skybase.actions.skychef.get_stack_chef_nodes(
                    skybase_stack_id=skybase_stack_id,
                    runner_cfg=self.runner_cfg,
                )

        self.preflight_check_result.set_output(preflight_result)
        return self.preflight_check_result

    def execute(self):
        self.result.output = dict()
        self.result.format = skytask.output_format_json

        self.result.output['stack_deletion_info'] = self.stack_deletion_info
        self.result.output['chef_type'] = self.chef_type
        self.result.output['stack_chef_nodes'] = self.stack_chef_nodes

        self.result.output['delete_stacks'] = skybase.actions.skycloud.delete_stacks(
            planet=self.planet,
            runtime=self.runtime,
            stacks=self.stack_deletion_list,
        )

        self.result.output['delete_stack_objects'] = skybase.actions.skycloud.delete_stack_objects(
            planet=self.planet,
            runtime=self.runtime,
            stacks=self.stack_deletion_info,
        )

        if self.is_chef_type_server:
            self.result.output['delete_stack_chef_roles'] = skybase.actions.skychef.delete_stack_chef_roles(
                planet=self.planet,
                runtime=self.runtime,
                stacks=self.stack_deletion_info,
            )

            self.result.output['delete_stack_chef_nodes'] = skybase.actions.skychef.delete_stack_chef_nodes(
                planet=self.planet,
                runtime=self.runtime,
                stack_nodes=self.stack_chef_nodes,
            )

        if self.runtime.apply:
            self.result.next_task_name = 'service.delete_stacks_state'
            self.result.next_args = {
                'planet_name': self.args.get('planet_name'),
                'service_name': self.args.get('service_name'),
                'tag': self.args.get('tag'),
                'stacks': self.stack_deletion_info,
                'apply': self.runtime.apply,
            }

        return self.result
