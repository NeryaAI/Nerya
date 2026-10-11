"""SDK facade for the same funds gateway used by native tools and the UI."""
from ..financial.contracts import context_from_config
from ..financial.gateway import FinancialGateway


class FinancialAPI:
    def __init__(self,config,*,context_provider=None,validate_resource=None,read_only=False):
        self.config=config;self.context_provider=context_provider;self.validate_resource=validate_resource;self.read_only=read_only
    def _context(self,*,write=False):
        if write and self.read_only:
            from ..financial.contracts import FinancialError
            raise FinancialError("paper_run_denies_funds_execution",403)
        if self.context_provider is not None:return self.context_provider(write)
        return context_from_config(self.config,actor_id=self.config.get('runtime.financial_actor_id'),
            scopes=self.config.get('runtime.financial_actor_scopes',()))
    def prepare(self,request,*,client_request_id,parent_action_id=None):
        if self.validate_resource is not None:self.validate_resource(request)
        return FinancialGateway(self.config).prepare(self._context(write=True),request,action_key=client_request_id,parent_action_id=parent_action_id)
    def refresh(self,action_id,*,expected_revision):
        return FinancialGateway(self.config).refresh(self._context(write=True),action_id,expected_revision=expected_revision)
    def discard(self,action_id,*,expected_revision):
        return FinancialGateway(self.config).discard(self._context(write=True),action_id,expected_revision=expected_revision)
    def execute(self,action_id,*,quote_hash):
        return FinancialGateway(self.config).execute(self._context(write=True),action_id,quote_hash=quote_hash)
    def get(self,action_id):
        return FinancialGateway(self.config).store.get_action(action_id,self._context())
    def reconcile(self,action_id):
        return FinancialGateway(self.config).reconcile(self._context(),action_id)
    def components(self):
        from ..trading.components import trading_components
        return trading_components(self.config).describe()

    def readiness(self):
        self._context().require('read:funds')
        from ..financial.readiness import execution_readiness
        return execution_readiness(self.config)
    def account_state(self,request):
        if self.validate_resource is not None:self.validate_resource(request)
        return FinancialGateway(self.config).account_state(self._context(),request)

    def prepare_rebalance(self, plan, *, client_request_id):
        from ..financial.rebalance import RebalanceService
        if self.validate_resource is not None and isinstance(plan, dict):
            for key in ('exit','swap','entry'):
                if isinstance(plan.get(key), dict):
                    self.validate_resource(plan[key])
        return RebalanceService(self.config).create(self._context(write=True), plan, client_key=client_request_id)

    def advance_rebalance(self, rebalance_id, *, expected_revision):
        from ..financial.rebalance import RebalanceService
        return RebalanceService(self.config).advance(self._context(write=True), rebalance_id, expected_revision=expected_revision)

    def get_rebalance(self, rebalance_id):
        from ..financial.rebalance import RebalanceService
        return RebalanceService(self.config).get(self._context(), rebalance_id)

    def stop_rebalance(self, rebalance_id, *, expected_revision):
        from ..financial.rebalance import RebalanceService
        return RebalanceService(self.config).stop(self._context(write=True), rebalance_id, expected_revision=expected_revision)
