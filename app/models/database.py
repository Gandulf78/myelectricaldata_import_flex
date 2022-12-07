import json
import os
import hashlib
from datetime import datetime, timedelta

from sqlalchemy import (create_engine, delete, inspect, select)
from sqlalchemy.orm import sessionmaker

from config import MAX_IMPORT_TRY
from db_schema import (
    Config,
    Contracts,
    Addresses,
    UsagePoints,
    ConsumptionDaily,
    ProductionDaily,
    ConsumptionDetail,
    ProductionDetail,
)
from dependencies import str2bool
from models.config import get_version
from models.log import Log

LOG = Log()


class Database:
    def __init__(self, path="/data"):
        self.path = path
        self.db_name = "cache.db"
        self.db_path = f"{self.path}/{self.db_name}"
        self.uri = f'sqlite:///{self.db_path}?check_same_thread=False'

        self.engine = create_engine(self.uri, echo=False, query_cache_size=0)
        self.session = sessionmaker(self.engine)(autocommit=True, autoflush=True)
        self.inspector = inspect(self.engine)

        # MIGRATE v7 to v8
        if os.path.isfile(f"{self.path}/enedisgateway.db"):
            LOG.title_warning("Migration de l'ancienne base de données vers la nouvelle structure.")
            self.migratev7tov8()

    def migratev7tov8(self):
        uri = f'sqlite:///{self.path}/enedisgateway.db'
        engine = create_engine(uri, echo=False, query_cache_size=0)
        session = sessionmaker(engine)(autocommit=True, autoflush=True)

        for measurement_direction in ["consumption", "production"]:
            LOG.warning(f'Migration des "{measurement_direction}_daily"')
            if measurement_direction == "consumption":
                table = ConsumptionDaily
            else:
                table = ProductionDaily
            daily_data = session.execute(f"select * from {measurement_direction}_daily order by date").all()
            current_date = ""
            year_value = 0
            bulk_insert = []
            for daily in daily_data:
                usage_point_id = daily[0]
                date = datetime.strptime(daily[1], "%Y-%m-%d")
                value = daily[2]
                year_value = year_value + value
                bulk_insert.append(table(
                    usage_point_id=usage_point_id,
                    date=date,
                    value=value,
                    blacklist=0,
                    fail_count=0
                ))
                if current_date != date.strftime("%Y"):
                    LOG.warning(f" - {date.strftime('%Y')} => {round(year_value / 1000, 2)}kW")
                    current_date = date.strftime("%Y")
                    year_value = 0
            self.session.add_all(bulk_insert)

            LOG.warning(f'Migration des "{measurement_direction}_detail"')
            if measurement_direction == "consumption":
                table = ConsumptionDetail
            else:
                table = ProductionDetail
            detail_data = session.execute(f"select * from {measurement_direction}_detail order by date").all()
            current_date = ""
            day_value = 0
            bulk_insert = []
            for detail in detail_data:
                usage_point_id = detail[0]
                date = datetime.strptime(detail[1], "%Y-%m-%d %H:%M:%S") - timedelta(minutes=30)
                value = detail[2]
                interval = detail[3]
                measure_type = detail[4]
                day_value = day_value + value / (60 / interval)
                bulk_insert.append(table(
                    usage_point_id=usage_point_id,
                    date=date,
                    value=value,
                    interval=interval,
                    measure_type=measure_type,
                    blacklist=0,
                    fail_count=0
                ))
                if current_date != date.strftime("%m"):
                    LOG.warning(f" - {date.strftime('%Y-%m')} => {round(day_value / 1000, 2)}kW")
                    current_date = date.strftime("%m")
                    day_value = 0
            self.session.add_all(bulk_insert)

        os.replace(f"{self.path}/enedisgateway.db", f"{self.path}/enedisgateway.db.migrate")

        # sys.exit()

    def init_database(self):
        LOG.log("Configure Databases")
        query = select(Config).where(Config.key == "day")
        day = self.session.scalars(query).one_or_none()
        if day:
            day.value = datetime.now().strftime('%Y-%m-%d')
        else:
            self.session.add(Config(key="day", value=datetime.now().strftime('%Y-%m-%d')))
        LOG.log(" => day")
        query = select(Config).where(Config.key == "call_number")
        if not self.session.scalars(query).one_or_none():
            self.session.add(Config(key="call_number", value="0"))
        LOG.log(" => call_number")
        query = select(Config).where(Config.key == "max_call")
        if not self.session.scalars(query).one_or_none():
            self.session.add(Config(key="max_call", value="500"))
        LOG.log(" => max_call")
        query = select(Config).where(Config.key == "version")
        version = self.session.scalars(query).one_or_none()
        if version:
            version.value = get_version()
        else:
            self.session.add(Config(key="version", value=get_version()))
        LOG.log(" => version")
        query = select(Config).where(Config.key == "lock")
        if not self.session.scalars(query).one_or_none():
            self.session.add(Config(key="lock", value="0"))
        LOG.log(" => lock")
        query = select(Config).where(Config.key == "lastUpdate")
        if not self.session.scalars(query).one_or_none():
            self.session.add(Config(key="lastUpdate", value=str(datetime.now())))
        LOG.log(" => lastUpdate")
        LOG.log(" Success")

    def purge_database(self):
        LOG.separator_warning()
        LOG.log("Reset SQLite Database")
        if os.path.exists(f'{self.path}/cache.db'):
            os.remove(f"{self.path}/cache.db")
            LOG.log(" => Success")
        else:
            LOG.log(" => Not cache detected")

    def lock_status(self):
        query = select(Config).where(Config.key == "lock")
        if str(self.session.scalars(query).one_or_none()) == "1":
            return True
        else:
            return False

    def lock(self):
        query = select(Config).where(Config.key == "lock")
        lock = self.session.scalars(query).one_or_none()
        lock.value = "1"
        return self.lock_status()

    def unlock(self):
        query = select(Config).where(Config.key == "lock")
        lock = self.session.scalars(query).one_or_none()
        lock.value = "0"
        return self.lock_status()

    ## ----------------------------------------------------------------------------------------------------------------
    ## CONFIG
    ## ----------------------------------------------------------------------------------------------------------------
    def get_config(self, key):
        query = select(Config).where(Config.key == key)
        return self.session.scalars(query).one_or_none()

    def set_config(self, key, value):
        query = select(Config).where(Config.key == key)
        config = self.session.scalars(query).one_or_none()
        if config:
            config.value = json.dumps(value)
        else:
            self.session.add(Config(key=key, value=json.dumps(value)))
        self.session.expire_all()

    ## ----------------------------------------------------------------------------------------------------------------
    ## USAGE POINTS
    ## ----------------------------------------------------------------------------------------------------------------
    def get_usage_point_all(self):
        query = select(UsagePoints)
        return self.session.scalars(query).all()

    def get_usage_point(self, usage_point_id):
        query = select(UsagePoints).where(UsagePoints.usage_point_id == usage_point_id)
        return self.session.scalars(query).one_or_none()

    def set_usage_point(self, usage_point_id, data):
        query = (
            select(UsagePoints)
            .where(UsagePoints.usage_point_id == usage_point_id)
        )
        usage_points = self.session.scalars(query).one_or_none()
        if "enable" in data:
            enable = data["enable"]
        else:
            enable = True
        if "name" in data:
            name = data["name"]
        else:
            name = ""
        if "cache" in data:
            cache = data["cache"]
        else:
            cache = True
        if "consumption" in data:
            consumption = data["consumption"]
        else:
            consumption = True
        if "consumption_detail" in data:
            consumption_detail = data["consumption_detail"]
        else:
            consumption_detail = True
        if "production" in data:
            production = data["production"]
        else:
            production = False
        if "production_detail" in data:
            production_detail = data["production_detail"]
        else:
            production_detail = False
        if "production_price" in data:
            production_price = data["production_price"]
        else:
            production_price = 0
        if "consumption_price_base" in data:
            consumption_price_base = data["consumption_price_base"]
        else:
            consumption_price_base = 0
        if "consumption_price_hc" in data:
            consumption_price_hc = data["consumption_price_hc"]
        else:
            consumption_price_hc = 0
        if "consumption_price_hp" in data:
            consumption_price_hp = data["consumption_price_hp"]
        else:
            consumption_price_hp = 0
        if "offpeak_hours_0" in data:
            offpeak_hours_0 = data["offpeak_hours_0"]
        else:
            offpeak_hours_0 = ""
        if "offpeak_hours_1" in data:
            offpeak_hours_1 = data["offpeak_hours_1"]
        else:
            offpeak_hours_1 = ""
        if "offpeak_hours_2" in data:
            offpeak_hours_2 = data["offpeak_hours_2"]
        else:
            offpeak_hours_2 = ""
        if "offpeak_hours_3" in data:
            offpeak_hours_3 = data["offpeak_hours_3"]
        else:
            offpeak_hours_3 = ""
        if "offpeak_hours_4" in data:
            offpeak_hours_4 = data["offpeak_hours_4"]
        else:
            offpeak_hours_4 = ""
        if "offpeak_hours_5" in data:
            offpeak_hours_5 = data["offpeak_hours_5"]
        else:
            offpeak_hours_5 = ""
        if "offpeak_hours_6" in data:
            offpeak_hours_6 = data["offpeak_hours_6"]
        else:
            offpeak_hours_6 = ""
        if "plan" in data:
            plan = data["plan"]
        else:
            plan = "BASE"
        if "refresh_addresse" in data:
            refresh_addresse = data["refresh_addresse"]
        else:
            refresh_addresse = False
        if "refresh_contract" in data:
            refresh_contract = data["refresh_contract"]
        else:
            refresh_contract = False
        if "token" in data:
            token = data["token"]
        else:
            token = ""
        progress = 0
        if "progress" in data:
            progress = data["progress"]
        progress_status = ""
        if "progress_status" in data:
            progress_status = data["progress_status"]
        consumption_max_date = None
        if "consumption_max_date" in data:
            if not data["consumption_max_date"] or data["consumption_max_date"] is None:
                consumption_max_date = None
            else:
                consumption_max_date = data["consumption_max_date"]
                if not isinstance(consumption_max_date, datetime):
                    consumption_max_date = datetime.strptime(consumption_max_date, "%Y-%m-%d")
        consumption_detail_max_date = None
        if "consumption_detail_max_date" in data:
            if "consumption_detail_max_date" in data:
                if not data["consumption_detail_max_date"] or data["consumption_detail_max_date"] is None:
                    consumption_detail_max_date = None
                else:
                    consumption_detail_max_date = data["consumption_detail_max_date"]
                    if not isinstance(consumption_detail_max_date, datetime):
                        consumption_detail_max_date = datetime.strptime(consumption_detail_max_date,"%Y-%m-%d")
        production_max_date = None
        if "production_max_date" in data:
            if not data["production_max_date"] or data["production_max_date"] is None:
                production_max_date = None
            else:
                production_max_date = data["production_max_date"]
                if not isinstance(production_max_date, datetime):
                    production_max_date = datetime.strptime(production_max_date, "%Y-%m-%d")
        production_detail_max_date = None
        if "production_detail_max_date" in data:
            if not data["production_detail_max_date"] or data["production_detail_max_date"] is None:
                production_detail_max_date = None
            else:
                production_detail_max_date = data["production_detail_max_date"]
                if isinstance(production_detail_max_date, datetime):
                    production_detail_max_date = production_detail_max_date
                else:
                    production_detail_max_date = datetime.strptime(production_detail_max_date, "%Y-%m-%d")

        if "call_number" in data:
            call_number = data["call_number"]
        else:
            call_number = 0
        if "quota_reached" in data:
            quota_reached = str2bool(data["quota_reached"])
        else:
            quota_reached = False
        if "quota_limit" in data:
            quota_limit = data["quota_limit"]
        else:
            quota_limit = 0
        if "quota_reset_at" in data:
            quota_reset_at = data["quota_reset_at"]
        else:
            quota_reset_at = None
        if "last_call" in data:
            last_call = data["last_call"]
        else:
            last_call = None
        if "ban" in data:
            ban = str2bool(data["ban"])
        else:
            ban = False
        if "consentement_expiration" in data:
            consentement_expiration = data["consentement_expiration"]
        else:
            consentement_expiration = None

        if usage_points is not None:
            usage_points.enable = str2bool(enable)
            usage_points.name = name
            usage_points.cache = str2bool(cache)
            usage_points.consumption = str2bool(consumption)
            usage_points.consumption_detail = str2bool(consumption_detail)
            usage_points.production = str2bool(production)
            usage_points.production_detail = str2bool(production_detail)
            usage_points.production_price = production_price
            usage_points.consumption_price_base = consumption_price_base
            usage_points.consumption_price_hc = consumption_price_hc
            usage_points.consumption_price_hp = consumption_price_hp
            usage_points.offpeak_hours_0 = offpeak_hours_0
            usage_points.offpeak_hours_1 = offpeak_hours_1
            usage_points.offpeak_hours_2 = offpeak_hours_2
            usage_points.offpeak_hours_3 = offpeak_hours_3
            usage_points.offpeak_hours_4 = offpeak_hours_4
            usage_points.offpeak_hours_5 = offpeak_hours_5
            usage_points.offpeak_hours_6 = offpeak_hours_6
            usage_points.offpeak_hours_6 = offpeak_hours_6
            usage_points.plan = plan
            usage_points.refresh_addresse = str2bool(refresh_addresse)
            usage_points.refresh_contract = str2bool(refresh_contract)
            usage_points.token = token
            usage_points.progress = progress
            usage_points.progress_status = progress_status
            usage_points.consumption_max_date = consumption_max_date
            usage_points.consumption_detail_max_date = consumption_detail_max_date
            usage_points.production_max_date = production_max_date
            usage_points.production_detail_max_date = production_detail_max_date
            usage_points.call_number = call_number
            usage_points.quota_reached = str2bool(quota_reached)
            usage_points.quota_limit = quota_limit
            usage_points.quota_reset_at = quota_reset_at
            usage_points.last_call = last_call
            usage_points.ban = str2bool(ban)
            usage_points.consentement_expiration = consentement_expiration
        else:
            self.session.add(
                UsagePoints(
                    usage_point_id=usage_point_id,
                    name=name,
                    cache=str2bool(cache),
                    consumption=str2bool(consumption),
                    consumption_detail=str2bool(consumption_detail),
                    production=str2bool(production),
                    production_detail=str2bool(production_detail),
                    production_price=production_price,
                    consumption_price_base=consumption_price_base,
                    consumption_price_hc=consumption_price_hc,
                    consumption_price_hp=consumption_price_hp,
                    offpeak_hours_0=offpeak_hours_0,
                    offpeak_hours_1=offpeak_hours_1,
                    offpeak_hours_2=offpeak_hours_2,
                    offpeak_hours_3=offpeak_hours_3,
                    offpeak_hours_4=offpeak_hours_4,
                    offpeak_hours_5=offpeak_hours_5,
                    offpeak_hours_6=offpeak_hours_6,
                    plan=plan,
                    refresh_addresse=str2bool(refresh_addresse),
                    refresh_contract=str2bool(refresh_contract),
                    token=token,
                    progress=progress,
                    progress_status=progress_status,
                    enable=str2bool(enable),
                    consumption_max_date=consumption_max_date,
                    consumption_detail_max_date=consumption_detail_max_date,
                    production_max_date=production_max_date,
                    production_detail_max_date=production_detail_max_date,
                    call_number=call_number,
                    quota_reached=str2bool(quota_reached),
                    quota_limit=quota_limit,
                    quota_reset_at=quota_reset_at,
                    last_call=last_call,
                    ban=str2bool(ban),
                    consentement_expiration=consentement_expiration
                )
            )

    def progress(self, usage_point_id, increment):
        query = (
            select(UsagePoints)
            .where(UsagePoints.usage_point_id == usage_point_id)
        )
        usage_points = self.session.scalars(query).one_or_none()
        usage_points.progress = usage_points.progress + increment

    def usage_point_update(self,
                           usage_point_id,
                           consentement_expiration=datetime.now(),
                           call_number=0,
                           quota_reached=False,
                           quota_limit=None,
                           quota_reset_at=None,
                           last_call=None,
                           ban=None
                           ):
        query = (
            select(UsagePoints)
            .where(UsagePoints.usage_point_id == usage_point_id)
        )
        usage_points = self.session.scalars(query).one_or_none()
        usage_points.consentement_expiration = consentement_expiration
        usage_points.call_number = call_number
        usage_points.quota_reached = quota_reached
        usage_points.quota_limit = quota_limit
        usage_points.quota_reset_at = quota_reset_at
        usage_points.last_call = last_call
        usage_points.ban = ban

    ## ----------------------------------------------------------------------------------------------------------------
    ## ADDRESSES
    ## ----------------------------------------------------------------------------------------------------------------
    def get_addresse(self, usage_point_id):
        query = (
            select(Addresses)
            .join(UsagePoints.relation_addressess)
            .where(UsagePoints.usage_point_id == usage_point_id)
        )
        return self.session.scalars(query).one_or_none()

    def set_addresse(self, usage_point_id, data, count=0):
        query = (
            select(Addresses)
            .join(UsagePoints.relation_addressess)
            .where(Addresses.usage_point_id == usage_point_id)
        )
        addresses = self.session.scalars(query).one_or_none()
        if addresses is not None:
            addresses.street = data["street"]
            addresses.locality = data["locality"]
            addresses.postal_code = data["postal_code"]
            addresses.insee_code = data["insee_code"]
            addresses.city = data["city"]
            addresses.country = data["country"]
            addresses.geo_points = data["geo_points"]
            addresses.count = count
        else:
            self.session.add(
                Addresses(
                    usage_point_id=usage_point_id,
                    street=data["street"],
                    locality=data["locality"],
                    postal_code=data["postal_code"],
                    insee_code=data["insee_code"],
                    city=data["city"],
                    country=data["country"],
                    geo_points=data["geo_points"],
                    count=count)
            )

    ## ----------------------------------------------------------------------------------------------------------------
    ## CONTRACTS
    ## ----------------------------------------------------------------------------------------------------------------
    def get_contract(self, usage_point_id):
        query = (
            select(Contracts)
            .join(UsagePoints.relation_contract)
            .where(UsagePoints.usage_point_id == usage_point_id)
        )
        return self.session.scalars(query).one_or_none()

    def set_contract(
            self,
            usage_point_id,
            data,
            count=0,
    ):
        query = (
            select(Contracts)
            .join(UsagePoints.relation_contract)
            .where(UsagePoints.usage_point_id == usage_point_id)
        )
        contract = self.session.scalars(query).one_or_none()
        if contract is not None:
            contract.usage_point_status = data['usage_point_status']
            contract.meter_type = data['meter_type']
            contract.segment = data['segment']
            contract.subscribed_power = data['subscribed_power']
            contract.last_activation_date = data['last_activation_date']
            contract.distribution_tariff = data['distribution_tariff']
            contract.offpeak_hours_0 = data['offpeak_hours_0']
            contract.offpeak_hours_1 = data['offpeak_hours_1']
            contract.offpeak_hours_2 = data['offpeak_hours_2']
            contract.offpeak_hours_3 = data['offpeak_hours_3']
            contract.offpeak_hours_4 = data['offpeak_hours_4']
            contract.offpeak_hours_5 = data['offpeak_hours_5']
            contract.offpeak_hours_6 = data['offpeak_hours_6']
            contract.contract_status = data['contract_status']
            contract.last_distribution_tariff_change_date = data['last_distribution_tariff_change_date']
            contract.count = count
        else:
            self.session.add(
                Contracts(
                    usage_point_id=usage_point_id,
                    usage_point_status=data['usage_point_status'],
                    meter_type=data['meter_type'],
                    segment=data['segment'],
                    subscribed_power=data['subscribed_power'],
                    last_activation_date=data['last_activation_date'],
                    distribution_tariff=data['distribution_tariff'],
                    offpeak_hours_0=data['offpeak_hours_0'],
                    offpeak_hours_1=data['offpeak_hours_1'],
                    offpeak_hours_2=data['offpeak_hours_2'],
                    offpeak_hours_3=data['offpeak_hours_3'],
                    offpeak_hours_4=data['offpeak_hours_4'],
                    offpeak_hours_5=data['offpeak_hours_5'],
                    offpeak_hours_6=data['offpeak_hours_6'],
                    contract_status=data['contract_status'],
                    last_distribution_tariff_change_date=data['last_distribution_tariff_change_date'],
                    count=count
                )
            )

    ## ----------------------------------------------------------------------------------------------------------------
    ## DAILY
    ## ----------------------------------------------------------------------------------------------------------------
    def get_daily_all(self, usage_point_id, measurement_direction="consumption"):
        if measurement_direction == "consumption":
            table = ConsumptionDaily
            relation = UsagePoints.relation_consumption_daily
        else:
            table = ProductionDaily
            relation = UsagePoints.relation_production_daily
        return self.session.scalars(
            select(table)
            .join(relation)
            .where(UsagePoints.usage_point_id == usage_point_id)
            .order_by(table.date.desc())
        ).all()

    def get_daily_date(self, usage_point_id, date, measurement_direction="consumption"):
        unique_id = hashlib.md5(f"{usage_point_id}/{date}".encode('utf-8')).hexdigest()
        if measurement_direction == "consumption":
            table = ConsumptionDaily
            relation = UsagePoints.relation_consumption_daily
        else:
            table = ProductionDaily
            relation = UsagePoints.relation_production_daily
        return self.session.scalars(
            select(table)
            .join(relation)
            .where(table.id == unique_id)
        ).first()

    def get_daily_state(self, usage_point_id, date, measurement_direction="consumption"):
        if self.get_daily_date(usage_point_id, date, measurement_direction) is not None:
            return True
        else:
            return False

    def get_daily_last_date(self, usage_point_id, measurement_direction="consumption"):
        if measurement_direction == "consumption":
            table = ConsumptionDaily
            relation = UsagePoints.relation_consumption_daily
        else:
            table = ProductionDaily
            relation = UsagePoints.relation_production_daily
        current_data = self.session.scalars(
            select(table)
            .join(relation)
            .where(table.usage_point_id == usage_point_id)
            .order_by(table.date)
        ).first()
        if current_data is None:
            return False
        else:
            return current_data.date

    def get_daily_last(self, usage_point_id, measurement_direction="consumption"):
        if measurement_direction == "consumption":
            table = ConsumptionDaily
            relation = UsagePoints.relation_consumption_daily
        else:
            table = ProductionDaily
            relation = UsagePoints.relation_production_daily
        current_data = self.session.scalars(
            select(table)
            .join(relation)
            .where(table.usage_point_id == usage_point_id)
            .where(table.value != 0)
            .order_by(table.date.desc())
        ).first()
        if current_data is None:
            return False
        else:
            return current_data

    def get_daily_first_date(self, usage_point_id, measurement_direction="consumption"):
        if measurement_direction == "consumption":
            table = ConsumptionDaily
            relation = UsagePoints.relation_consumption_daily
        else:
            table = ProductionDaily
            relation = UsagePoints.relation_production_daily
        query = (
            select(table)
            .join(relation)
            .where(table.usage_point_id == usage_point_id)
            .order_by(table.date.desc())
        )
        LOG.debug(query.compile(compile_kwargs={"literal_binds": True}))
        current_data = self.session.scalars(query).first()
        if current_data is None:
            return False
        else:
            return current_data.date

    def get_daily_fail_count(self, usage_point_id, date, measurement_direction="consumption"):
        result = self.get_daily_date(usage_point_id, date, measurement_direction)
        if hasattr(result, "fail_count"):
            return result.fail_count
        else:
            return 0

    def daily_fail_increment(self, usage_point_id, date, measurement_direction="consumption"):
        unique_id = hashlib.md5(f"{usage_point_id}/{date}".encode('utf-8')).hexdigest()
        if measurement_direction == "consumption":
            table = ConsumptionDaily
            relation = UsagePoints.relation_consumption_daily
        else:
            table = ProductionDaily
            relation = UsagePoints.relation_production_daily
        query = (select(table)
                 .join(relation)
                 .where(table.id == unique_id))
        LOG.debug(query.compile(compile_kwargs={"literal_binds": True}))
        daily = self.session.scalars(query).one_or_none()
        if daily is not None:
            fail_count = int(daily.fail_count) + 1
            if fail_count >= MAX_IMPORT_TRY:
                blacklist = 1
                fail_count = 0
            else:
                blacklist = 0
            daily.id = unique_id
            daily.usage_point_id = usage_point_id
            daily.date = date
            daily.value = 0
            daily.blacklist = blacklist
            daily.fail_count = fail_count
        else:
            fail_count = 0
            self.session.add(
                table(
                    id=unique_id,
                    usage_point_id=usage_point_id,
                    date=date,
                    value=0,
                    blacklist=0,
                    fail_count=0,
                )
            )
        return fail_count

    def get_daily_range(self, usage_point_id, begin, end, measurement_direction="consumption"):
        if measurement_direction == "consumption":
            table = ConsumptionDaily
            relation = UsagePoints.relation_consumption_daily
        else:
            table = ProductionDaily
            relation = UsagePoints.relation_production_daily
        query = (
            select(table)
            .join(relation)
            .where(table.usage_point_id == usage_point_id)
            .where(table.date >= begin)
            .where(table.date <= end)
            .order_by(table.date.desc())
        )
        LOG.debug(query.compile(compile_kwargs={"literal_binds": True}))
        current_data = self.session.scalars(query).all()
        if current_data is None:
            return False
        else:
            return current_data

    def get_daily(self, usage_point_id, begin, end, measurement_direction="consumption"):
        delta = end - begin
        result = {
            "missing_data": False,
            "date": {},
            "count": 0
        }
        for i in range(delta.days + 1):
            checkDate = begin + timedelta(days=i)
            checkDate = datetime.combine(checkDate, datetime.min.time())
            query_result = self.get_daily_date(usage_point_id, checkDate, measurement_direction)
            checkDate = checkDate.strftime('%Y-%m-%d')
            if query_result is None:
                # NEVER QUERY
                result["date"][checkDate] = {
                    "status": False,
                    "blacklist": 0,
                    "value": 0
                }
                result["missing_data"] = True
            else:
                consumption = query_result.value
                blacklist = query_result.blacklist
                if consumption == 0:
                    # ENEDIS RETURN NO DATA
                    result["date"][checkDate] = {
                        "status": False,
                        "blacklist": blacklist,
                        "value": consumption
                    }
                    result["missing_data"] = True
                else:
                    # SUCCESS or BLACKLIST
                    result["date"][checkDate] = {
                        "status": True,
                        "blacklist": blacklist,
                        "value": consumption
                    }
        return result

    def insert_daily(self, usage_point_id, date, value, blacklist=0, fail_count=0,
                     measurement_direction="consumption"):
        unique_id = hashlib.md5(f"{usage_point_id}/{date}".encode('utf-8')).hexdigest()
        if measurement_direction == "consumption":
            table = ConsumptionDaily
            relation = UsagePoints.relation_consumption_daily
        else:
            table = ProductionDaily
            relation = UsagePoints.relation_production_daily
        query = (select(table)
                 .join(relation)
                 .where(table.id == unique_id))
        daily = self.session.scalars(query).one_or_none()
        LOG.debug(query.compile(compile_kwargs={"literal_binds": True}))
        if daily is not None:
            daily.id = unique_id
            daily.usage_point_id = usage_point_id
            daily.date = date
            daily.value = value
            daily.blacklist = blacklist
            daily.fail_count = fail_count
        else:
            self.session.add(
                table(
                    id=unique_id,
                    usage_point_id=usage_point_id,
                    date=date,
                    value=value,
                    blacklist=blacklist,
                    fail_count=fail_count,
                )
            )

    def delete_daily(self, usage_point_id, date=None, measurement_direction="consumption"):
        if measurement_direction == "consumption":
            table = ConsumptionDaily
        else:
            table = ProductionDaily
        if date is not None:
            unique_id = hashlib.md5(f"{usage_point_id}/{date}".encode('utf-8')).hexdigest()
            self.session.execute(
                delete(table)
                .where(table.id == unique_id)
            )
        else:
            self.session.execute(delete(table).where(table.usage_point_id == usage_point_id))
        return True

    def blacklist_daily(self, usage_point_id, date, action=True, measurement_direction="consumption"):
        unique_id = hashlib.md5(f"{usage_point_id}/{date}".encode('utf-8')).hexdigest()
        if measurement_direction == "consumption":
            table = ConsumptionDaily
            relation = UsagePoints.relation_consumption_daily
        else:
            table = ProductionDaily
            relation = UsagePoints.relation_production_daily
        query = (select(table)
                 .join(relation)
                 .where(table.id == unique_id)
                 )
        daily = self.session.scalars(query).one_or_none()
        if daily is not None:
            daily.blacklist = action
        else:
            self.session.add(
                table(
                    id=unique_id,
                    usage_point_id=usage_point_id,
                    date=date,
                    value=0,
                    blacklist=action,
                    fail_count=0,
                )
            )
        return True

    def get_daily_date_range(self, usage_point_id):
        return {
            "begin": self.get_daily_last_date(usage_point_id),
            "end": self.get_daily_first_date(usage_point_id)
        }

    ## -----------------------------------------------------------------------------------------------------------------
    ## DETAIL CONSUMPTION
    ## -----------------------------------------------------------------------------------------------------------------
    def get_detail_all(self, usage_point_id, begin=None, end=None, measurement_direction="consumption"):
        if measurement_direction == "consumption":
            table = ConsumptionDetail
            relation = UsagePoints.relation_consumption_detail
        else:
            table = ProductionDetail
            relation = UsagePoints.relation_production_detail
        if begin is None or end is None:
            return self.session.scalars(
                select(table)
                .join(relation)
                .where(table.usage_point_id == usage_point_id)
                .order_by(table.date)
            ).all()
        else:
            return self.session.scalars(
                select(table)
                .join(relation)
                .where(table.usage_point_id == usage_point_id)
                .filter(table.date <= end)
                .filter(table.date >= begin)
                .order_by(table.date)
            ).all()

    def get_detail_date(self, usage_point_id, date, measurement_direction="consumption"):
        unique_id = hashlib.md5(f"{usage_point_id}/{date}".encode('utf-8')).hexdigest()
        if measurement_direction == "consumption":
            table = ConsumptionDetail
            relation = UsagePoints.relation_consumption_detail
        else:
            table = ProductionDetail
            relation = UsagePoints.relation_production_detail
        return self.session.scalars(
            select(table)
            .join(relation)
            .where(table.id == unique_id)
        ).first()

    def get_detail_range(self, usage_point_id, begin, end, measurement_direction="consumption"):
        if measurement_direction == "consumption":
            table = ConsumptionDetail
            relation = UsagePoints.relation_consumption_detail
        else:
            table = ProductionDetail
            relation = UsagePoints.relation_production_detail
        query = (
            select(table)
            .join(relation)
            .where(table.usage_point_id == usage_point_id)
            .where(table.date >= begin)
            .where(table.date <= end)
            .order_by(table.date.desc())
        )
        LOG.debug(query.compile(compile_kwargs={"literal_binds": True}))
        current_data = self.session.scalars(query).all()
        if current_data is None:
            return False
        else:
            return current_data

    def get_detail(self, usage_point_id, begin, end, measurement_direction="consumption"):

        # begin = datetime.combine(begin, datetime.min.time())
        # end = datetime.combine(end, datetime.max.time())

        delta = begin - begin

        result = {
            "missing_data": False,
            "date": {},
            "count": 0
        }

        for i in range(delta.days + 1):
            query_result = self.get_detail_all(usage_point_id, begin, end, measurement_direction)
            time_delta = abs(int((begin - end).total_seconds() / 60))
            total_internal = 0
            for query in query_result:
                total_internal = total_internal + query.interval
            total_time = abs(total_internal - time_delta)
            if total_time > 300:
                LOG.log(f" - {total_time}m absente du relevé.")
                result["missing_data"] = True
            else:
                for query in query_result:
                    result["date"][query.date] = {
                        "value": query.value,
                        "interval": query.interval,
                        "measure_type": query.measure_type,
                        "blacklist": query.blacklist,
                    }
            return result

    def get_detail_state(self, usage_point_id, date, measurement_direction="consumption"):
        unique_id = hashlib.md5(f"{usage_point_id}/{date}".encode('utf-8')).hexdigest()
        if measurement_direction == "consumption":
            table = ConsumptionDetail
            relation = UsagePoints.relation_consumption_detail
        else:
            table = ProductionDetail
            relation = UsagePoints.relation_production_detail
        current_data = self.session.scalars(
            select(table)
            .join(relation)
            .where(table.id == unique_id)
        ).one_or_none()
        if current_data is None:
            return False
        else:
            return True

    # def insert_detail_bulk(self, data, mesure_type="consumption"):
    #     if mesure_type == "consumption":
    #         table = ConsumptionDetail
    #     else:
    #         table = ProductionDetail
    #     begin = ""
    #     end = ""
    #     for scalar in data:
    #         if begin == "":
    #             begin = scalar.date
    #         end = scalar.date
    #     self.session.execute(
    #         table.__table__.delete().filter(ConsumptionDetail.date.between(begin, end))
    #     )
    #     self.session.add_all(data)

    def insert_detail(self, usage_point_id, date, value, interval, measure_type, blacklist=0, fail_count=0,
                      mesure_type="consumption"):
        unique_id = hashlib.md5(f"{usage_point_id}/{date}".encode('utf-8')).hexdigest()
        if mesure_type == "consumption":
            table = ConsumptionDetail
        else:
            table = ProductionDetail
        detail = self.get_detail_date(usage_point_id, date, mesure_type)
        if detail is not None:
            detail.id = unique_id
            detail.usage_point_id = usage_point_id
            detail.date = date
            detail.value = value
            detail.interval = interval
            detail.measure_type = measure_type
            detail.blacklist = blacklist
            detail.fail_count = fail_count
        else:
            self.session.add(
                table(
                    id=unique_id,
                    usage_point_id=usage_point_id,
                    date=date,
                    value=value,
                    interval=interval,
                    measure_type=measure_type,
                    blacklist=blacklist,
                    fail_count=fail_count,
                )
            )

    def delete_detail(self, usage_point_id, date=None, mesure_type="consumption"):
        if mesure_type == "consumption":
            table = ConsumptionDetail
        else:
            table = ProductionDetail
        if date is not None:
            unique_id = hashlib.md5(f"{usage_point_id}/{date}".encode('utf-8')).hexdigest()
            self.session.execute(
                delete(table)
                .where(table.id == unique_id)
            )
        else:
            self.session.execute(delete(table).where(table.usage_point_id == usage_point_id))
        return True

    def get_ratio_hc_hp(self, usage_point_id, begin, end, mesure_type="consumption"):
        result = {
            "HC": 0,
            "HP": 0,
        }
        detail_data = self.get_detail_all(usage_point_id, begin, end, mesure_type)
        for data in detail_data:
            result[data.measure_type] = result[data.measure_type] + data.value
        return result

    def get_detail_fail_count(self, usage_point_id, date, mesure_type="consumption"):
        return self.get_detail_date(usage_point_id, date, mesure_type).fail_count

    def detail_fail_increment(self, usage_point_id, date, mesure_type="consumption"):
        unique_id = hashlib.md5(f"{usage_point_id}/{date}".encode('utf-8')).hexdigest()
        if mesure_type == "consumption":
            table = ConsumptionDetail
            relation = UsagePoints.relation_consumption_detail
        else:
            table = ProductionDetail
            relation = UsagePoints.relation_production_detail
        query = (select(table)
                 .join(relation)
                 .where(table.id == unique_id))
        detail = self.session.scalars(query).one_or_none()
        if detail is not None:
            fail_count = int(detail.fail_count) + 1
            if fail_count >= MAX_IMPORT_TRY:
                blacklist = 1
                fail_count = 0
            else:
                blacklist = 0
            detail.usage_point_id = usage_point_id
            detail.date = date
            detail.value = 0
            detail.interval = 0
            detail.measure_type = "HP"
            detail.blacklist = blacklist
            detail.fail_count = fail_count
        else:
            fail_count = 0
            self.session.add(
                table(
                    id=unique_id,
                    usage_point_id=usage_point_id,
                    date=date,
                    value=0,
                    interval=0,
                    measure_type="HP",
                    blacklist=0,
                    fail_count=0,
                )
            )
        return fail_count

    def get_detail_last_date(self, usage_point_id, mesure_type="consumption"):
        if mesure_type == "consumption":
            table = ConsumptionDetail
            relation = UsagePoints.relation_consumption_detail
        else:
            table = ProductionDetail
            relation = UsagePoints.relation_production_detail
        current_data = self.session.scalars(
            select(table)
            .join(relation)
            .where(table.usage_point_id == usage_point_id)
            .order_by(table.date)
        ).first()
        if current_data is None:
            return False
        else:
            return current_data.date

    def get_detail_first_date(self, usage_point_id, mesure_type="consumption"):
        if mesure_type == "consumption":
            table = ConsumptionDetail
            relation = UsagePoints.relation_consumption_detail
        else:
            table = ProductionDetail
            relation = UsagePoints.relation_production_detail
        query = (
            select(table)
            .join(relation)
            .where(table.usage_point_id == usage_point_id)
            .order_by(table.date.desc())
        )
        LOG.debug(query.compile(compile_kwargs={"literal_binds": True}))
        current_data = self.session.scalars(query).first()
        if current_data is None:
            return False
        else:
            return current_data.date

    def get_detail_date_range(self, usage_point_id):
        return {
            "begin": self.get_detail_last_date(usage_point_id),
            "end": self.get_detail_first_date(usage_point_id)
        }


os.system("cd /app; alembic upgrade head")
Database().init_database()