import base64
import json
from google.api_core.exceptions import NotFound
from google.cloud import bigquery
from google.cloud import storage
import re
import os
import tempfile
import logging
from datetime import datetime
from http import HTTPStatus
import pandas as pd


class InputValidator(object):
    def __init__(self, event):
        try:
            # validate input message
            # extract information from message payload
            message_payload = json.loads(base64.b64decode(event['data']).decode('utf-8'))
            bq_destination_table = \
                message_payload['protoPayload']['serviceData']['jobCompletedEvent']['job']['jobConfiguration']['load'][
                    'destinationTable']
            self.gcp_project = bq_destination_table['projectId']
            self.dataset = bq_destination_table['datasetId']
            self.table_date_shard = re.search(r'_(20\d\d\d\d\d\d)$', bq_destination_table['tableId']).group(1)
            self.table_name = re.search(r'(events.*)_20\d\d\d\d\d\d$', bq_destination_table['tableId']).group(1)
        except AttributeError:
            logging.critical(f'invalid message: {message_payload}')
        try:
            storage_client = storage.Client()
            bucket = storage_client.bucket(os.environ["CONFIG_BUCKET_NAME"])
            blob = bucket.blob(os.environ["CONFIG_FILENAME"])
            downloaded_file = os.path.join(tempfile.gettempdir(), "tmp.json")
            blob.download_to_filename(downloaded_file)
            with open(downloaded_file, "r") as config_json:
                self.config = json.load(config_json)
        except Exception as e:
            logging.critical(f'flattener configuration error: {e}')

    def valid_dataset(self):
        return self.dataset in self.config.keys()

    def flatten_nested_table(self, nested_table):
        return nested_table in self.config[self.dataset]["tables_to_flatten"]

    def get_output_configuration(self):
        """
        Extract info from the config file on whether we want sharded output, partitioned output or both
        :return:
        """
        config_output = self.config[self.dataset].get("output", {
            "sharded": True,
            "partitioned": False})

        return config_output


class GaExportedNestedDataStorage(object):

    # suffix used to denote field names in flat_events which were created from event parameter keys
    EP_SUFFIX = '_ep'

    def __init__(self, gcp_project, dataset, table_name, date_shard, type='DAILY'):#TODO: set this to INTRADAY for intraday flattening

        # main configurations
        self.gcp_project = gcp_project
        self.dataset = dataset
        self.date_shard = date_shard
        self.date = datetime.strptime(self.date_shard, '%Y%m%d')
        self.table_name = table_name
        self.type = type

        # The next several properties will correspond to GA4 fields

        # event parameters to be pivoted into columns in flat_events
        self.event_params_flat_fields = {}

        # These fields will be used to build a compound id of a unique event
        # stream_id is added to make sure that there is definitely no id collisions, if you have multiple data streams
        self.unique_event_id_fields = [
            "stream_id",
            "user_pseudo_id",
            "event_name",
            "event_timestamp"
        ]

        # event parameters
        self.event_params_fields = [
            "event_params.key",

            "event_params.value.string_value",
            "event_params.value.int_value",
            "event_params.value.float_value",
            "event_params.value.double_value"
        ]

        # user properties
        self.user_properties_fields = [
            "user_properties.key",

            "user_properties.value.string_value",
            "user_properties.value.int_value",
            "user_properties.value.float_value",
            "user_properties.value.double_value",
            "user_properties.value.set_timestamp_micros"
        ]

        # items
        self.items_fields = [
            "items.item_id",
            "items.item_name",
            "items.item_brand",
            "items.item_variant",
            "items.item_category",
            "items.item_category2",
            "items.item_category3",
            "items.item_category4",
            "items.item_category5",
            "items.price_in_usd",
            "items.price",
            "items.quantity",
            "items.item_revenue_in_usd",
            "items.item_revenue",
            "items.item_refund_in_usd",
            "items.item_refund",
            "items.coupon",
            "items.affiliation",
            "items.location_id",
            "items.item_list_id",
            "items.item_list_name",
            "items.item_list_index",
            "items.promotion_id",
            "items.promotion_name",
            "items.creative_name",
            "items.creative_slot"
        ]

        # events
        self.events_fields = [
            "event_date",
            "event_timestamp",
            "event_name",
            "event_previous_timestamp",
            "event_value_in_usd",
            "event_bundle_sequence_id",
            "event_server_timestamp_offset",
            "user_id",
            "user_pseudo_id",

            "privacy_info.analytics_storage",
            "privacy_info.ads_storage",
            "privacy_info.uses_transient_token",
            "user_first_touch_timestamp",

            "user_ltv.revenue",
            "user_ltv.currency",

            "device.category",
            "device.mobile_brand_name",
            "device.mobile_model_name",
            "device.mobile_marketing_name",
            "device.mobile_os_hardware_model",
            "device.operating_system",
            "device.operating_system_version",
            "device.vendor_id",
            "device.advertising_id",
            "device.language",
            "device.is_limited_ad_tracking",
            "device.time_zone_offset_seconds",
            "device.browser",
            "device.browser_version",

            "device.web_info.browser",
            "device.web_info.browser_version",
            "device.web_info.hostname",

            "geo.continent",
            "geo.country",
            "geo.region",
            "geo.city",
            "geo.sub_continent",
            "geo.metro",

            "app_info.id",
            "app_info.version",
            "app_info.install_store",
            "app_info.firebase_app_id",
            "app_info.install_source",

            "traffic_source.name",
            "traffic_source.medium",
            "traffic_source.source",
            "stream_id",
            "platform",

            "event_dimensions.hostname",

            "ecommerce.total_item_quantity",
            "ecommerce.purchase_revenue_in_usd",
            "ecommerce.purchase_revenue",
            "ecommerce.refund_value_in_usd",
            "ecommerce.refund_value",
            "ecommerce.shipping_value_in_usd",
            "ecommerce.shipping_value",
            "ecommerce.tax_value_in_usd",
            "ecommerce.tax_value",
            "ecommerce.unique_items",
            "ecommerce.transaction_id"
        ]

        self.partitioned_table_schemas = {
            "flat_event_params": [
                bigquery.SchemaField("event_date", bigquery.enums.SqlTypeNames.DATE),
                bigquery.SchemaField("event_id", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("event_params_key", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("event_params_value", bigquery.enums.SqlTypeNames.STRING),
            ],

            "flat_events": [
                bigquery.SchemaField("event_date", bigquery.enums.SqlTypeNames.DATE),
                bigquery.SchemaField("event_id", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("event_timestamp", bigquery.enums.SqlTypeNames.INTEGER),
                bigquery.SchemaField("event_name", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("event_previous_timestamp", bigquery.enums.SqlTypeNames.INTEGER),
                bigquery.SchemaField("event_value_in_usd", bigquery.enums.SqlTypeNames.FLOAT),
                bigquery.SchemaField("event_bundle_sequence_id", bigquery.enums.SqlTypeNames.INTEGER),
                bigquery.SchemaField("event_server_timestamp_offset", bigquery.enums.SqlTypeNames.INTEGER),
                bigquery.SchemaField("user_id", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("user_pseudo_id", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("privacy_info_analytics_storage", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("privacy_info_ads_storage", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("privacy_info_uses_transient_token", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("user_first_touch_timestamp", bigquery.enums.SqlTypeNames.INTEGER),
                bigquery.SchemaField("user_ltv_revenue", bigquery.enums.SqlTypeNames.FLOAT),
                bigquery.SchemaField("user_ltv_currency", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("device_category", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("device_mobile_brand_name", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("device_mobile_model_name", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("device_mobile_marketing_name", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("device_mobile_os_hardware_model", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("device_operating_system", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("device_operating_system_version", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("device_vendor_id", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("device_advertising_id", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("device_language", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("device_is_limited_ad_tracking", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("device_time_zone_offset_seconds", bigquery.enums.SqlTypeNames.INTEGER),
                bigquery.SchemaField("device_browser", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("device_browser_version", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("device_web_info_browser", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("device_web_info_browser_version", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("device_web_info_hostname", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("geo_continent", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("geo_country", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("geo_region", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("geo_city", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("geo_sub_continent", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("geo_metro", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("app_info_id", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("app_info_version", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("app_info_install_store", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("app_info_firebase_app_id", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("app_info_install_source", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("traffic_source_name", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("traffic_source_medium", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("traffic_source_source", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("stream_id", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("platform", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("event_dimensions_hostname", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("ecommerce_total_item_quantity", bigquery.enums.SqlTypeNames.INTEGER),
                bigquery.SchemaField("ecommerce_purchase_revenue_in_usd", bigquery.enums.SqlTypeNames.FLOAT),
                bigquery.SchemaField("ecommerce_purchase_revenue", bigquery.enums.SqlTypeNames.FLOAT),
                bigquery.SchemaField("ecommerce_refund_value_in_usd", bigquery.enums.SqlTypeNames.FLOAT),
                bigquery.SchemaField("ecommerce_refund_value", bigquery.enums.SqlTypeNames.FLOAT),
                bigquery.SchemaField("ecommerce_shipping_value_in_usd", bigquery.enums.SqlTypeNames.FLOAT),
                bigquery.SchemaField("ecommerce_shipping_value", bigquery.enums.SqlTypeNames.FLOAT),
                bigquery.SchemaField("ecommerce_tax_value_in_usd", bigquery.enums.SqlTypeNames.FLOAT),
                bigquery.SchemaField("ecommerce_tax_value", bigquery.enums.SqlTypeNames.FLOAT),
                bigquery.SchemaField("ecommerce_unique_items", bigquery.enums.SqlTypeNames.INTEGER),
                bigquery.SchemaField("ecommerce_transaction_id", bigquery.enums.SqlTypeNames.STRING),
            ],

            "flat_items": [
                bigquery.SchemaField("event_date", bigquery.enums.SqlTypeNames.DATE),
                bigquery.SchemaField("event_id", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("items_item_id", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("items_item_name", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("items_item_brand", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("items_item_variant", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("items_item_category", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("items_item_category2", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("items_item_category3", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("items_item_category4", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("items_item_category5", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("items_price_in_usd", bigquery.enums.SqlTypeNames.FLOAT),
                bigquery.SchemaField("items_price", bigquery.enums.SqlTypeNames.FLOAT),
                bigquery.SchemaField("items_quantity", bigquery.enums.SqlTypeNames.INTEGER),
                bigquery.SchemaField("items_item_revenue_in_usd", bigquery.enums.SqlTypeNames.FLOAT),
                bigquery.SchemaField("items_item_revenue", bigquery.enums.SqlTypeNames.FLOAT),
                bigquery.SchemaField("items_item_refund_in_usd", bigquery.enums.SqlTypeNames.FLOAT),
                bigquery.SchemaField("items_item_refund", bigquery.enums.SqlTypeNames.FLOAT),
                bigquery.SchemaField("items_coupon", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("items_affiliation", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("items_location_id", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("items_item_list_id", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("items_item_list_name", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("items_item_list_index", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("items_promotion_id", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("items_promotion_name", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("items_creative_name", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("items_creative_slot", bigquery.enums.SqlTypeNames.STRING),
            ],

            "flat_user_properties": [
                bigquery.SchemaField("event_date", bigquery.enums.SqlTypeNames.DATE),
                bigquery.SchemaField("event_id", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("user_properties_key", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("user_properties_value", bigquery.enums.SqlTypeNames.STRING),
                bigquery.SchemaField("user_properties_value_set_timestamp_micros", bigquery.enums.SqlTypeNames.INTEGER),
            ],
        }

        self.partitioning_column = "event_date"

    def get_unique_event_id(self, unique_event_id_fields):
        """
        build unique event id
        """
        return 'CONCAT(%s, "_", %s, "_", %s, "_", %s) as event_id' % (unique_event_id_fields[0],
                                                                      unique_event_id_fields[1],
                                                                      unique_event_id_fields[2],
                                                                      unique_event_id_fields[3])

    def get_event_params_keys_and_types_query(self):
        # get distinct set of event parameter keys from shard event table to dynamically extend flat_events schema
        qry = """
                WITH T as (
                  SELECT event_params.key as event_params_key, 
                  CONCAT(IF(event_params.value.string_value IS NULL, '','STRING'),
                         IF(event_params.value.int_value IS NULL, '', 'INTEGER'), 
                         IF(event_params.value.double_value IS NULL, '', 'DOUBLE'), 
                         IF(event_params.value.float_value IS NULL, '', 'FLOAT')
                        )  AS event_params_type
                """

        qry += " FROM `{p}.{ds}.{t}_{d}`".format(p=self.gcp_project, ds=self.dataset, t=self.table_name,
                                                 d=self.date_shard)

        qry += ", UNNEST (event_params) AS event_params) SELECT DISTINCT(event_params_key), event_params_type FROM T"

        return qry

    def get_event_params_query(self):
        qry = "SELECT "

        # get unique event id
        qry += self.get_unique_event_id(self.unique_event_id_fields)

        qry += ",%s as %s" % (self.event_params_fields[0], self.event_params_fields[0].replace(".", "_"))

        qry += ",CONCAT(IFNULL(%s, ''), IFNULL(CAST(%s AS STRING), ''), IFNULL(CAST(%s AS STRING), ''), IFNULL(CAST(%s AS STRING), '')) AS event_params_value" \
               % (self.event_params_fields[1], self.event_params_fields[2], self.event_params_fields[3],
                  self.event_params_fields[4])

        qry += " FROM `{p}.{ds}.{t}_{d}`".format(p=self.gcp_project, ds=self.dataset, t=self.table_name,
                                                 d=self.date_shard)

        qry += ",UNNEST (event_params) AS event_params"

        return qry

    def get_user_properties_query(self):
        qry = "SELECT "

        # get unique event id
        qry += self.get_unique_event_id(self.unique_event_id_fields)

        qry += ",%s as %s" % (self.user_properties_fields[0], self.user_properties_fields[0].replace(".", "_"))

        qry += ",CONCAT(IFNULL(%s, ''), IFNULL(CAST(%s AS STRING), ''), IFNULL(CAST(%s AS STRING), ''), IFNULL(CAST(%s AS STRING), '')) AS user_properties_value" \
               % (self.user_properties_fields[1], self.user_properties_fields[2], self.user_properties_fields[3],
                  self.user_properties_fields[4])

        qry += ",%s as %s" % (self.user_properties_fields[5], self.user_properties_fields[5].replace(".", "_"))

        qry += " FROM `{p}.{ds}.{t}_{d}`".format(p=self.gcp_project, ds=self.dataset, t=self.table_name,
                                                 d=self.date_shard)

        qry += ",UNNEST (user_properties) AS user_properties"

        return qry

    def get_items_query(self):
        qry = "SELECT "

        # get unique event id
        qry += self.get_unique_event_id(self.unique_event_id_fields)

        for f in self.items_fields:
            qry += ",%s as %s" % (f, f.replace(".", "_"))

        qry += " FROM `{p}.{ds}.{t}_{d}`".format(p=self.gcp_project, ds=self.dataset, t=self.table_name,
                                                 d=self.date_shard)

        qry += ",UNNEST (items) AS items"

        return qry

    def get_events_query(self):
        qry = "SELECT "

        # get unique event id
        qry += self.get_unique_event_id(self.unique_event_id_fields)

        for f in self.events_fields:
            qry += ",%s as %s" % (f, f.replace(".", "_"))

        # using list of flat event params field from set_dynamic_flat_events_schema()
        for key, f_type in self.event_params_flat_fields.items():
            if f_type == 'INTEGER':
                f_type = 'int'
            qry += ",(SELECT value.%s_value FROM UNNEST(events.event_params) WHERE key = '%s') AS %s%s" % (
                f_type.lower(), key, key, self.EP_SUFFIX)

        qry += " FROM `{p}.{ds}.{t}_{d}` as events".format(p=self.gcp_project, ds=self.dataset, t=self.table_name,
                                                           d=self.date_shard)
        return qry

    def _create_valid_bigquery_field_name(self, p_field):
        '''
        Creates a valid BigQuery field name
        BQ Fields must contain only letters, numbers, and underscores, start with a letter or underscore,
        and be at most 300 characters long.
        :param p_field: starting point of the field
        :return: cleaned big query field name
        '''
        r = ""  # initialize emptry string
        for char in p_field.lower():
            if char.isalnum():
                # if char is alphanumeric (either letters or numbers), append char to our string
                r += char
            else:
                # otherwise, replace it with underscore
                r += "_"
        # if field starts with digit, prepend it with underscore
        if r[0].isdigit():
            r = "_%s" % r
        return r[:300]  # trim the string to the first x chars

    def transform_dataframe(self, dataframe, table_type):
        """

        Transforms the dataframe which will be loaded into the partitioned table.

        Adds a timestamp column into a dataframe, which will be used for partitioning.

        Ensures the right column order.

        Ensures the right data types, so they match the data types in the sharded table.

        If we don't run this function, then wrong data types may be loaded into BQ,
            even if you request the required data types in load job config schema.
        """

        # add date field to the dataframe

        dataframe[self.partitioning_column] = self.date

        # if a pandas column has missing values
        # Because NaN is a float, this forces an array of integers with any missing values to become floating point.
        # you will have an error converting it from float to integer if you do df[[col]] = df[[col]].astype(int)
        # pandas.errors.IntCastingNaNError: Cannot convert non-finite values (NA or inf) to integer
        # In this case, even if you request that the field is BQ integer in BQ load job config, the field will still get loaded as float
        # therefore, we first need to force the field to integer in pandas (by default, it will be float, because np.Nan is float)
        # https://pandas.pydata.org/pandas-docs/stable/user_guide/integer_na.html
        # https://stackoverflow.com/questions/48511484/data-type-conversion-error-valueerror-cannot-convert-non-finite-values-na-or

        bq_schema = self.partitioned_table_schemas.get(table_type, None)

        for column in dataframe:
            for dest_bq_field in bq_schema:
                if dest_bq_field.name == column:
                    if dest_bq_field.field_type == "STRING":
                        dataframe[[column]] = dataframe[[column]].astype(str)
                    elif dest_bq_field.field_type == "FLOAT":
                        dataframe[[column]] = dataframe[[column]].astype(float)
                    elif dest_bq_field.field_type == "INTEGER":
                        dataframe[column] = pd.Series(list(dataframe[column]),
                                                      dtype=pd.Int64Dtype())
                    # https://www.statology.org/convert-datetime-to-date-pandas/
                    elif dest_bq_field.field_type == "DATE":
                        dataframe[column] = dataframe[column].dt.date

        # https://stackoverflow.com/questions/25122099/move-column-by-name-to-front-of-table-in-pandas
        col = dataframe[self.partitioning_column]
        dataframe.drop(labels=[self.partitioning_column], axis=1, inplace=True)
        dataframe.insert(0, self.partitioning_column, col)

        return dataframe

    def run_query_job(self, query, table_type, sharded_output_required=True, partitioned_output_required=False):

        """
        Depending on the configuration, we will write data to sharded table, partitioned table, or both.
        :param query:
        :param table_type:
        :return:
        """

        # TODO: this function is huge, split it into multiple functions???

        # 1
        # QUERY AND FLATTEN DATA. WRITE SHARDED OUTPUT, if flattener is configured to do so

        client = bigquery.Client()  # initialize BigQuery client

        # get table name
        table_name = "{p}.{ds}.{t}_{d}" \
            .format(p=self.gcp_project, ds=self.dataset, t=table_type, d=self.date_shard)

        table_id = bigquery.Table(table_name)

        # configure query job
        query_job_flatten_config = bigquery.QueryJobConfig(
            # we will query and flatten the data ,
            # but we may or may not write the result to a sharded table,
            # depending on the config
            destination=table_id if sharded_output_required else None
            , dry_run=False
            # when a destination table is specified in the job configuration, query results are not cached
            # https://cloud.google.com/bigquery/docs/cached-results
            , use_query_cache=True
            , labels={"queryfunction": "flatteningquery"}  # todo: apply proper labels
            , write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE)

        # run the job
        query_job_flatten = client.query(query,
                                         job_config=query_job_flatten_config)
        # we may or may not save query result into into a pandas dataframe and write into a partitioned table,
        # depending on the config
        if partitioned_output_required:
            # 2
            # WRITE PARTITIONED OUTPUT, if flattener is configured to do so
            # BQ -> pandas df
            query_job_flatten_result = query_job_flatten.result()  # Waits for job to complete.
            # # https://cloud.google.com/bigquery/docs/bigquery-storage-python-pandas#download_query_results_using_the_client_library
            dataframe = query_job_flatten_result.to_dataframe()  # we will need this dataframe if we load data to a partitioned table

            dataframe = self.transform_dataframe(dataframe, table_type=table_type)

            try:
                # delete the partition, if it already exists, before we load it
                # this ensures that we don't have dupes
                datetime_string = str(self.date)
                date_string = re.search(r'20\d\d\-\d\d\-\d\d', datetime_string).group(0)

                query_delete = """
                           DELETE FROM `{p}.{ds}.{t}` WHERE event_date = "{date_shard}";
                       """.format(p=self.gcp_project, ds=self.dataset, t=table_type, date_shard=date_string)

                query_job_delete_config = bigquery.QueryJobConfig(
                    labels={"queryfunction": "flattenerpartitiondeletionquery"}  # todo: apply proper labels
                )
                query_job_delete = client.query(query_delete,
                                                job_config=query_job_delete_config)  # Make an API request.
                query_job_delete.result()  # Waits for job to complete.

            except Exception as e:
                if e.code == HTTPStatus.NOT_FOUND:  # 404 Not found
                    logging.warning(f"Cannot delete the partition because the table doesn't exist yet: {e}")
                else:
                    logging.critical(f"Cannot delete the partition: {e}")
            # pandas df -> BQ
            # https://cloud.google.com/bigquery/docs/samples/bigquery-load-table-dataframe

            load_job_config_partitioned = bigquery.LoadJobConfig(
                # Specify a (partial) schema. All columns are always written to the
                # table. The schema is used to assist in data type definitions.
                schema=self.partitioned_table_schemas.get(table_type, None),
                autodetect=False if self.partitioned_table_schemas.get(table_type, None) else True,
                # https://stackoverflow.com/questions/59430708/how-to-load-dataframe-into-bigquery-partitioned-table-from-cloud-function-with-p
                time_partitioning=bigquery.TimePartitioning(
                    type_=bigquery.TimePartitioningType.DAY,
                    field=self.partitioning_column  # field to use for partitioning
                ),
                # Optionally, set the write disposition. BigQuery appends loaded rows
                # to an existing table by default, but with WRITE_TRUNCATE write
                # disposition it replaces the table with the loaded data.
                write_disposition="WRITE_APPEND"
                , labels={"queryfunction": "flattenerpartitionloadjob"}
            )

            table_name_partitioned = "{p}.{ds}.{t}" \
                .format(p=self.gcp_project, ds=self.dataset, t=table_type)
            table_id_partitioned = bigquery.Table(table_name_partitioned)

            load_job_partition = client.load_table_from_dataframe(
                dataframe=dataframe, destination=table_id_partitioned, job_config=load_job_config_partitioned
            )  # Make an API request.
            load_job_partition.result()  # Wait for the job to complete.

    def set_dynamic_flat_events_schema(self):

        # Get all event_params_keys and data types from original GA4 sharded table
        query = self.get_event_params_keys_and_types_query()

        client = bigquery.Client()
        query_job_config = bigquery.QueryJobConfig(
            dry_run=False)
        query_job_schema = client.query(query, job_config=query_job_config)
        event_params_rows = query_job_schema.result()

        # get existing flat_events table schema
        flat_events_table = "{p}.{ds}.flat_events".format(p=self.gcp_project, ds=self.dataset)
        try:
            table = client.get_table(flat_events_table)
            original_schema = table.schema
        except NotFound:
            original_schema = self.partitioned_table_schemas['flat_events']
            logging.info('flat_events table not found, using hardcoded schema')

        # check all existing flat_events schema fields and add any event_params to list used in flat_events query
        for schema_field in original_schema:
            if schema_field.name.endswith(self.EP_SUFFIX):
                # add the raw field name and type for use in get_events_query() without suffic (to get raw params)
                original_field_name = schema_field.name[:len(schema_field.name) - len(self.EP_SUFFIX)]  # remove suffix
                self.event_params_flat_fields[original_field_name] = schema_field.field_type

        # add any new event_params_keys from raw data to list used in flat_events query
        for row in event_params_rows:
            if row.event_params_key not in self.event_params_flat_fields:
                self.event_params_flat_fields[row.event_params_key] = row.event_params_type

        new_schema = original_schema[:]
        # append new event_params fields to original schema with new suffix to avoid collisions
        for ep_key, ep_type in self.event_params_flat_fields.items():
            field_name = '{epk}{suf}'.format(epk=ep_key, suf=self.EP_SUFFIX)
            new_schema.append(bigquery.SchemaField(field_name, ep_type))

        self.partitioned_table_schemas['flat_events'] = new_schema


def flatten_ga_data(event, context):
    """
    Flatten GA4 data
    Triggered from a message on a Cloud Pub/Sub topic.
    Args:
         event (dict): Event payload.
         context (google.cloud.functions.Context): Metadata for the event.
    """
    input_event = InputValidator(event)
    output_config = input_event.get_output_configuration()
    output_config_sharded = output_config["sharded"]
    output_config_partitioned = output_config["partitioned"]

    if input_event.valid_dataset():
        ga_source = GaExportedNestedDataStorage(gcp_project=input_event.gcp_project,
                                                dataset=input_event.dataset,
                                                table_name=input_event.table_name,
                                                date_shard=input_event.table_date_shard)

        # set dynamic flat events schema
        ga_source.set_dynamic_flat_events_schema()

        # EVENT_PARAMS
        if input_event.flatten_nested_table(nested_table=os.environ["EVENT_PARAMS"]):
            ga_source.run_query_job(query=ga_source.get_event_params_query(), table_type="flat_event_params",
                                    sharded_output_required=output_config_sharded,
                                    partitioned_output_required=output_config_partitioned)
            logging.info(f'Ran {os.environ["EVENT_PARAMS"]} flattening query for {input_event.dataset}')
        else:
            logging.info(
                f'{os.environ["EVENT_PARAMS"]} flattening query for {input_event.dataset} not configured to run')

        # USER_PROPERTIES
        if input_event.flatten_nested_table(nested_table=os.environ["USER_PROPERTIES"]):
            ga_source.run_query_job(query=ga_source.get_user_properties_query(), table_type="flat_user_properties",
                                    sharded_output_required=output_config_sharded,
                                    partitioned_output_required=output_config_partitioned)
            logging.info(f'Ran {os.environ["USER_PROPERTIES"]} flattening query for {input_event.dataset}')
        else:
            logging.info(
                f'{os.environ["USER_PROPERTIES"]} flattening query for {input_event.dataset} not configured to run')

        # ITEMS
        if input_event.flatten_nested_table(nested_table=os.environ["ITEMS"]):
            ga_source.run_query_job(query=ga_source.get_items_query(), table_type="flat_items",
                                    sharded_output_required=output_config_sharded,
                                    partitioned_output_required=output_config_partitioned)
            logging.info(f'Ran {os.environ["ITEMS"]} flattening query for {input_event.dataset}')
        else:
            logging.info(f'{os.environ["ITEMS"]} flattening query for {input_event.dataset} not configured to run')

        # EVENTS
        if input_event.flatten_nested_table(nested_table=os.environ["EVENTS"]):
            ga_source.run_query_job(query=ga_source.get_events_query(), table_type="flat_events",
                                    sharded_output_required=output_config_sharded,
                                    partitioned_output_required=output_config_partitioned)
            logging.info(f'Ran {os.environ["EVENTS"]} flattening query for {input_event.dataset}')
        else:
            logging.info(f'{os.environ["EVENTS"]} flattening query for {input_event.dataset} not configured to run')

    else:
        logging.warning(f'Dataset {input_event.dataset} not configured for flattening')
