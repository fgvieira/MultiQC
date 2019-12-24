#!/usr/bin/env python
from __future__ import print_function

import itertools
import re
from collections import OrderedDict, defaultdict
from multiqc import config
from multiqc.modules.base_module import BaseMultiqcModule
from multiqc.plots import bargraph, beeswarm

# Initialise the logger
import logging
log = logging.getLogger(__name__)


class DragenMappingMetics(BaseMultiqcModule):
    def parse_mapping_metrics(self):
        data_by_rg_by_sample = defaultdict(dict)
        data_by_phenotype_by_sample = defaultdict(dict)

        for f in self.find_log_files('dragen/mapping_metrics'):
            data_by_readgroup, data_by_phenotype = parse_mapping_metrics_file(f)

            if f['s_name'] in data_by_rg_by_sample:
                log.debug('Duplicate Dragen output prefix found! Overwriting: {}'.format(f['s_name']))
                self.add_data_source(f, section='stats')
            data_by_phenotype_by_sample[f['s_name']] = data_by_phenotype

            for rg, data in data_by_readgroup.items():
                if any(rg in d_rg for sn, d_rg in data_by_rg_by_sample.items()):
                    log.debug('Duplicate read group name {} found for output prefix {}! Overwriting'.format(rg, f['s_name']))
            data_by_rg_by_sample[f['s_name']] = data_by_readgroup

        # filter to strip out ignored sample names:
        data_by_rg_by_sample = self.ignore_samples(data_by_rg_by_sample)
        data_by_phenotype_by_sample = self.ignore_samples(data_by_phenotype_by_sample)
        if not data_by_rg_by_sample and not data_by_phenotype_by_sample:
            return
        log.info("Found mapping metrics for {} Dragen output prefixes".format(len(data_by_rg_by_sample)))

        # flattening phenotype-sample data by adding a prefix " normal" to the normal samples
        data_by_sample = dict()
        for sn in data_by_phenotype_by_sample:
            for phenotype in data_by_phenotype_by_sample[sn]:
                new_sn = sn
                if phenotype == 'normal':
                    new_sn = sn + ' normal'
                data_by_sample[new_sn] = data_by_phenotype_by_sample[sn][phenotype]

        # general metrics that differrent between different samples are to be moved into per-sample stats,
        # those that are common for all samples will stay to be reported in the report header section
        maybe_add_general_metrics(data_by_sample)

        # merging all read group data
        data_by_rg = dict()
        for sname in data_by_rg_by_sample:
            for rg, d in data_by_rg_by_sample[sname].items():
                if rg in data_by_rg:
                    rg = rg + ' (' + sname + ')'
                data_by_rg[rg] = d

        # getting all available metric names to determine table headers
        all_metric_names = set()
        for sn, data_by_rg in data_by_rg_by_sample.items():
            for rg, data in data_by_rg.items():
                for m in data.keys():
                    all_metric_names.add(m)

        # and making headers
        headers, beeswarm_keys = make_mapping_stats_headers(all_metric_names)

        self.general_stats_addcols(data_by_sample, headers, 'Mapping metrics')
        # Make bargraph plots of mapped, dupped and paired reads
        self.__map_dup_read_chart(data_by_rg)
        self.__map_pair_read_chart(data_by_rg)

        self.add_section(
            name='Mapping metrics per RG',
            anchor='dragen-mapping-metrics',
            description="A dot plot showing DRAGEN mapping metrics for each input read group. "
                        "All read counts in " + str(config.read_count_desc) + ", " +
                        "all bases counts in " + str(config.read_count_desc),
            plot=beeswarm.plot(data_by_rg, beeswarm_keys, {'id': 'dragen-mapping-metrics-dp'})
        )

    def __map_dup_read_chart(self, data_by_sample):
        chart_data = dict()
        for sample_id, data in data_by_sample.items():
            if data['Number of unique & mapped reads (excl. duplicate marked reads)']\
                    + data['Number of duplicate marked reads']\
                    + data['Unmapped reads'] != data['Total reads in RG']:
                log.warning("sum of unique/duplicate/unmapped reads not matching total, "
                            "skipping mapping/duplicates percentages plot for: {}".format(sample_id))
            else:
                chart_data[sample_id] = data
        self.add_section(
            name='Mapped and duplicated per RG',
            anchor='dragen-mapping-dup-percentage',
            description='Mapping and duplicate reads per read group: '
                        'uniquely mapped vs. duplicate vs. unmapped reads.',
            plot=bargraph.plot(chart_data, {
                'Number of unique & mapped reads (excl. duplicate marked reads)': {'color': '#437bb1', 'name': 'Mapped'},
                'Number of duplicate marked reads':                               {'color': '#f5a742', 'name': 'Duplicated'},
                'Unmapped reads':                                                 {'color': '#b1084c', 'name': 'Unmapped'},
            }, {
                'id': 'mapping_dup_percentage_plot',
                'title': 'Mapping metrics per read group: duplicate reads',
                'ylab': '# Reads',
                'cpswitch_counts_label': 'Number of reads'
            })
        )

    def __map_pair_read_chart(self, data_by_sample):
        chart_data = dict()
        for sample_id, data in data_by_sample.items():
            if data['Not properly paired reads (discordant)'] + data['Properly paired reads']\
                    + data['Singleton reads (itself mapped; mate unmapped)']\
                    + data['Unmapped reads'] != data['Total reads in RG']:
                log.warning("sum of unpaired/discordant/proppaired/unmapped reads not matching total, "
                            "skipping mapping/paired percentages plot for: {}".format(sample_id))
            else:
                chart_data[sample_id] = data
        self.add_section(
            name='Mapped and paired per RG',
            anchor='dragen-mapping-paired-percentage',
            description="Mapping and pairing read metrics per read group: "
                        "properly paired vs. discordant vs. unpaired vs. unmapped reads.",
            plot=bargraph.plot(chart_data, {
                'Properly paired reads':                          {'color': '#00cc00', 'name': 'Paired, properly'},
                'Not properly paired reads (discordant)':         {'color': '#ff9900', 'name': 'Paired, discordant'},
                'Singleton reads (itself mapped; mate unmapped)': {'color': '#ff33cc', 'name': 'Singleton'},
                'Unmapped reads':                                 {'color': '#b1084c', 'name': 'Unmapped'},
            }, {
                'id': 'mapping_paired_percentage_plot',
                'title': '<apping metrics per read group: paired reads',
                'ylab': '# Reads',
                'cpswitch_counts_label': 'Number of reads'
            })
        )


def maybe_add_general_metrics(data_by_sample):
    general_stats_by_sample_by_metric = defaultdict(dict)

    for sname, data in data_by_sample.items():
        for metric, val in data.items():
            if metric in [metric for (metric, title, fmt, modify) in GENERAL_METRICS]:
                general_stats_by_sample_by_metric[metric][sname] = val

    for metric, title, fmt, modify in GENERAL_METRICS:
        vals_by_sample = general_stats_by_sample_by_metric[metric]
        # if general stats are shared for different samples, adding into the report header section:
        if len(list(set(vals_by_sample.values()))) == 1:
            if config.report_header_info is None:
                config.report_header_info = list()

            v = list(set(vals_by_sample.values()))[0]
            if v != 'NA':
                if modify:
                    v = modify(v)
                config.report_header_info.append({title: fmt.format(v)})

            # and removing them from general stats:
            for sname, data in data_by_sample.items():
                del data[metric]


def parse_mapping_metrics_file(f):
    """
    Mapping and aligning metrics, like the metrics computed by the Samtools Flagstat command, are available
    on an aggregate level (over all input data), and on a per read group level. Unless explicitly stated,
    the metrics units are in reads (ie, not in terms of pairs or alignments).

    T_SRR7890936_50pc.mapping_metrics.csv

    # phenotype-level metrics (tumor or normal):
    TUMOR MAPPING/ALIGNING SUMMARY,,Total input reads,2200000000,100.00
    TUMOR MAPPING/ALIGNING SUMMARY,,Number of duplicate marked reads,433637413,19.71
    TUMOR MAPPING/ALIGNING SUMMARY,,Number of duplicate marked and mate reads removed,NA
    TUMOR MAPPING/ALIGNING SUMMARY,,Number of unique reads (excl. duplicate marked reads),1766362587,80.29
    TUMOR MAPPING/ALIGNING SUMMARY,,Reads with mate sequenced,2200000000,100.00
    TUMOR MAPPING/ALIGNING SUMMARY,,Reads without mate sequenced,0,0.00
    TUMOR MAPPING/ALIGNING SUMMARY,,QC-failed reads,0,0.00
    TUMOR MAPPING/ALIGNING SUMMARY,,Mapped reads,2130883930,96.86
    TUMOR MAPPING/ALIGNING SUMMARY,,Mapped reads R1,1066701794,96.97
    TUMOR MAPPING/ALIGNING SUMMARY,,Mapped reads R2,1064182136,96.74
    TUMOR MAPPING/ALIGNING SUMMARY,,Number of unique & mapped reads (excl. duplicate marked reads),1697246517,77.15
    TUMOR MAPPING/ALIGNING SUMMARY,,Unmapped reads,69116070,3.14
    TUMOR MAPPING/ALIGNING SUMMARY,,Singleton reads (itself mapped; mate unmapped),3917092,0.18
    TUMOR MAPPING/ALIGNING SUMMARY,,Paired reads (itself & mate mapped),2126966838,96.68
    TUMOR MAPPING/ALIGNING SUMMARY,,Properly paired reads,2103060370,95.59
    TUMOR MAPPING/ALIGNING SUMMARY,,Not properly paired reads (discordant),23906468,1.09
    TUMOR MAPPING/ALIGNING SUMMARY,,Paired reads mapped to different chromosomes,17454370,0.82
    TUMOR MAPPING/ALIGNING SUMMARY,,Paired reads mapped to different chromosomes (MAPQ>=10),6463547,0.30
    TUMOR MAPPING/ALIGNING SUMMARY,,Reads with MAPQ [40:inf),2002661377,91.03
    TUMOR MAPPING/ALIGNING SUMMARY,,Reads with MAPQ [30:40),7169392,0.33
    TUMOR MAPPING/ALIGNING SUMMARY,,Reads with MAPQ [20:30),16644390,0.76
    TUMOR MAPPING/ALIGNING SUMMARY,,Reads with MAPQ [10:20),20280057,0.92
    TUMOR MAPPING/ALIGNING SUMMARY,,Reads with MAPQ [ 0:10),84128714,3.82
    TUMOR MAPPING/ALIGNING SUMMARY,,Reads with MAPQ NA (Unmapped reads),69116070,3.14
    TUMOR MAPPING/ALIGNING SUMMARY,,Reads with indel R1,26849051,2.52
    TUMOR MAPPING/ALIGNING SUMMARY,,Reads with indel R2,24810803,2.33
    TUMOR MAPPING/ALIGNING SUMMARY,,Total bases,330000000000
    TUMOR MAPPING/ALIGNING SUMMARY,,Total bases R1,165000000000
    TUMOR MAPPING/ALIGNING SUMMARY,,Total bases R2,165000000000
    TUMOR MAPPING/ALIGNING SUMMARY,,Mapped bases R1,160005269100
    TUMOR MAPPING/ALIGNING SUMMARY,,Mapped bases R2,159627320400
    TUMOR MAPPING/ALIGNING SUMMARY,,Soft-clipped bases R1,1757128997,1.10
    TUMOR MAPPING/ALIGNING SUMMARY,,Soft-clipped bases R2,3208748350,2.01
    TUMOR MAPPING/ALIGNING SUMMARY,,Mismatched bases R1,585802788,0.37
    TUMOR MAPPING/ALIGNING SUMMARY,,Mismatched bases R2,1155805091,0.72
    TUMOR MAPPING/ALIGNING SUMMARY,,Mismatched bases R1 (excl. indels),501394281,0.31
    TUMOR MAPPING/ALIGNING SUMMARY,,Mismatched bases R2 (excl. indels),1073788605,0.67
    TUMOR MAPPING/ALIGNING SUMMARY,,Q30 bases,297564555927,90.17
    TUMOR MAPPING/ALIGNING SUMMARY,,Q30 bases R1,155492239719,94.24
    TUMOR MAPPING/ALIGNING SUMMARY,,Q30 bases R2,142072316208,86.10
    TUMOR MAPPING/ALIGNING SUMMARY,,Q30 bases (excl. dups & clipped bases),246555769158
    TUMOR MAPPING/ALIGNING SUMMARY,,Total alignments,2190085267
    TUMOR MAPPING/ALIGNING SUMMARY,,Secondary alignments,0
    TUMOR MAPPING/ALIGNING SUMMARY,,Supplementary (chimeric) alignments,59201337
    TUMOR MAPPING/ALIGNING SUMMARY,,Estimated read length,150.00
    TUMOR MAPPING/ALIGNING SUMMARY,,Average sequenced coverage over genome,102.83
    TUMOR MAPPING/ALIGNING SUMMARY,,Insert length: mean,383.00
    TUMOR MAPPING/ALIGNING SUMMARY,,Insert length: median,376.00
    TUMOR MAPPING/ALIGNING SUMMARY,,Insert length: standard deviation,85.15
    # general metrics - reporting in the header (except for DRAGEN mapping rate may be different to T and N:
    TUMOR MAPPING/ALIGNING SUMMARY,,Bases in reference genome,3209286105
    TUMOR MAPPING/ALIGNING SUMMARY,,Bases in target bed [% of genome],NA
    TUMOR MAPPING/ALIGNING SUMMARY,,Provided sex chromosome ploidy,NA
    TUMOR MAPPING/ALIGNING SUMMARY,,DRAGEN mapping rate [mil. reads/second],0.39
    # then same for normal:
    NORMAL MAPPING/ALIGNING SUMMARY,,Total input reads,1100000000,100.00
    NORMAL MAPPING/ALIGNING SUMMARY,,Number of duplicate marked reads,123518125,11.23
    ...
    # then tumor and normal per-read-group metrics - reproting in the beeswarm plot:
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Total reads in RG,2200000000,100.00
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Number of duplicate marked reads,433637413,19.71
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Number of duplicate marked and mate reads removed,NA
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Number of unique reads (excl. duplicate marked reads),1766362587,80.29
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Reads with mate sequenced,2200000000,100.00
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Reads without mate sequenced,0,0.00
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,QC-failed reads,0,0.00
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Mapped reads,2130883930,96.86
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Mapped reads R1,1066701794,96.97
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Mapped reads R2,1064182136,96.74
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Number of unique & mapped reads (excl. duplicate marked reads),1697246517,77.15
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Unmapped reads,69116070,3.14
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Singleton reads (itself mapped; mate unmapped),3917092,0.18
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Paired reads (itself & mate mapped),2126966838,96.68
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Properly paired reads,2103060370,95.59
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Not properly paired reads (discordant),23906468,1.09
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Paired reads mapped to different chromosomes,17454370,0.82
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Paired reads mapped to different chromosomes (MAPQ>=10),6463547,0.30
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Reads with MAPQ [40:inf),2002661377,91.03
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Reads with MAPQ [30:40),7169392,0.33
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Reads with MAPQ [20:30),16644390,0.76
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Reads with MAPQ [10:20),20280057,0.92
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Reads with MAPQ [ 0:10),84128714,3.82
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Reads with MAPQ NA (Unmapped reads),69116070,3.14
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Reads with indel R1,26849051,2.52
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Reads with indel R2,24810803,2.33
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Total bases,330000000000
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Total bases R1,165000000000
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Total bases R2,165000000000
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Mapped bases R1,160005269100
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Mapped bases R2,159627320400
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Soft-clipped bases R1,1757128997,1.10
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Soft-clipped bases R2,3208748350,2.01
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Mismatched bases R1,585802788,0.37
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Mismatched bases R2,1155805091,0.72
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Mismatched bases R1 (excl. indels),501394281,0.31
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Mismatched bases R2 (excl. indels),1073788605,0.67
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Q30 bases,297564555927,90.17
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Q30 bases R1,155492239719,94.24
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Q30 bases R2,142072316208,86.10
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Q30 bases (excl. dups & clipped bases),246555769158
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Total alignments,2190085267
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Secondary alignments,0
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Supplementary (chimeric) alignments,59201337
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Estimated read length,150.00
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Average sequenced coverage over genome,102.83
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Insert length: mean,383.01
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Insert length: median,376.00
    TUMOR MAPPING/ALIGNING PER RG,T_SRR7890936_50pc,Insert length: standard deviation,87.58
    # same for normal:
    NORMAL MAPPING/ALIGNING PER RG,N_SRR7890889,Total reads in RG,1100000000,100.00
    NORMAL MAPPING/ALIGNING PER RG,N_SRR7890889,Number of duplicate marked reads,NA
    NORMAL MAPPING/ALIGNING PER RG,N_SRR7890889,Number of duplicate marked and mate reads removed,NA
    NORMAL MAPPING/ALIGNING PER RG,N_SRR7890889,Number of unique reads (excl. duplicate marked reads),NA
    ...

    We are reporting summary metrics in the general stats table, and per-read-group in the beeswarm plot.
    """

    f['s_name'] = re.search(r'(.*).mapping_metrics.csv', f['fn']).group(1)

    data_by_readgroup = defaultdict(dict)
    data_by_phenotype = defaultdict(dict)

    for line in f['f'].splitlines():
        fields = line.split(',')
        phenotype = fields[0].split('/')[0].split(' ')[0].lower()  # TUMOR MAPPING -> tumor
        analysis = fields[0].split('/')[1]  # ALIGNING SUMMARY, ALIGNING PER RG
        metric = fields[2]
        value = fields[3]
        try:
            value = int(value)
        except ValueError:
            try:
                value = float(value)
            except ValueError:
                pass

        percentage = None
        if len(fields) > 4:  # percentage
            percentage = fields[4]
            try:
                percentage = float(percentage)
            except ValueError:
                pass

        # sample-unspecific metrics are reported only in ALIGNING SUMMARY sections
        if analysis == 'ALIGNING SUMMARY':
            data_by_phenotype[phenotype][metric] = value
            if percentage is not None:
                data_by_phenotype[phenotype][metric + ' pct'] = percentage

        # for sample-specific metrics, using ALIGNING PER RG because it has the sample name in the 2nd col
        if analysis == 'ALIGNING PER RG':
            # setting normal and tumor sample names for future use
            readgroup = fields[1]
            data_by_readgroup[readgroup][metric] = value
            if percentage is not None:
                data_by_readgroup[readgroup][metric + ' pct'] = percentage

    # adding some missing values that we wanna report for consistency
    for data in itertools.chain(data_by_readgroup.values(), data_by_phenotype.values()):
        # fixing when deduplication wasn't performed
        if data['Number of duplicate marked reads'] == 'NA':
            data['Number of duplicate marked reads'] = 0
        if data['Number of duplicate marked and mate reads removed'] == 'NA':
            data['Number of duplicate marked and mate reads removed'] = 0
        if data['Number of unique reads (excl. duplicate marked reads)'] == 'NA':
            data['Number of unique reads (excl. duplicate marked reads)'] = data['Mapped reads']
        # adding alignment percentages
        if data['Total alignments'] > 0:
            data['Secondary alignments pct'] = data['Secondary alignments'] / data['Total alignments'] * 100.0
        # adding some missing bases percentages
        if data['Total bases'] > 0:
            data['Q30 bases (excl. dups & clipped bases) pct'] = data['Q30 bases (excl. dups & clipped bases)'] / data['Total bases'] * 100.0
            data['Mapped bases R1 pct'] = data['Mapped bases R1'] / data['Total bases'] * 100.0
            data['Mapped bases R2 pct'] = data['Mapped bases R2'] / data['Total bases'] * 100.0

    return data_by_readgroup, data_by_phenotype


read_format = '{:,.1f}'
if config.read_count_multiplier == 1:
    read_format = '{:,.0f}'
read_format += '&nbsp;' + config.read_count_prefix

base_format = '{:,.1f}&nbsp;'
if config.base_count_multiplier == 1:
    base_format = '{:,.0f}'
elif config.base_count_multiplier == 0.000000001:
    base_format = '{:,.2f}'
base_format += '&nbsp;' + config.base_count_prefix

MAPPING_METRICS = [
    # id_in_data                                               # title (display name)        # show  # unit  # beeswarm  # description
    # Read stats:
    ('Total input reads'                                      , 'Reads'                      , '#',  'reads', False, 'Total number of input reads for this sample (or total number of reads in all input read groups combined), {}'),
    ('Total reads in RG'                                      , 'Reads'                      , None, 'reads', True,  'Total number of reads in this RG, {}'),
    ('Reads with mate sequenced'                              , 'Reads with mate'            , None, 'reads', True,  'Number of reads with a mate sequenced, {}'),
    ('Reads without mate sequenced'                           , 'Reads w/o mate'             , None, 'reads', False, 'Number of reads without a mate sequenced, {}'),
    ('QC-failed reads'                                        , 'QC-fail'                    , None, 'reads', True,  'Number of reads not passing platform/vendor quality checks (SAM flag 0x200), {}'),
    ('Mapped reads'                                           , 'Map'                        , '%',  'reads', True,  'Number of mapped reads, {}'),
    ('Mapped reads R1'                                        , 'Map R1'                     , None, 'reads', False, 'Number of mapped reads R1, {}'),
    ('Mapped reads R2'                                        , 'Map R2'                     , None, 'reads', False, 'Number of mapped reads R2, {}'),
    ('Reads with MAPQ [40:inf)'                               , 'MQ>=40'                     , None, 'reads', True,  'Number of reads with MAPQ [40:inf), {}'),
    ('Number of duplicate marked reads'                       , 'Dup'                        , '%',  'reads', False, 'Number of duplicate marked reads as a result of the --enable-duplicatemarking option being used, {}'),
    ('Number of duplicate marked and mate reads removed'      , 'Dup with mates removed'     , None, 'reads', False, 'Number of reads marked as duplicates, along with any mate reads, that are removed '
                                                                                                                        'when the --remove-duplicates option is used, {}'),
    ('Number of unique reads (excl. duplicate marked reads)'  , 'Uniq'                       , None, 'reads', True,  'Number of unique reads (all reads minus duplicate marked reads), {}'),
    ('Number of unique & mapped reads (excl. duplicate marked reads)', 'Uniq map'            , None, 'reads', True,  'Number of unique & mapped reads (mapped reads minus duplicate marked reads), {}'),
    ('Unmapped reads'                                         , 'Unmap'                      , None, 'reads', False, 'Number of unmapped reads, {}'),
    ('Paired reads (itself & mate mapped)'                    , 'Self & mate mapped'         , None, 'reads', True,  'Number of reads mapped in pairs (itself & mate mapped), {}'),
    ('Properly paired reads'                                  , 'Prop pair'                  , '%',  'reads', True,  'Number of properly paired reads, {} (both reads in pair are mapped and '
                                                                                                                        'fall within an acceptable range from each other based on the estimated insert length distribution)'),
    ('Not properly paired reads (discordant)'                 , 'Discord'                    , None, 'reads', True,  'Number of discordant reads: paired reads minus properly paired reads , {}'),
    ('Singleton reads (itself mapped; mate unmapped)'         , 'Singleton'                  , None, 'reads', True,  'Number of singleton reads: itself mapped; mate unmapped, {}'),
    ('Paired reads mapped to different chromosomes'           , 'Mate map to diff chrom'     , None, 'reads', True,  'Number of paired reads with a mate mapped to a different chromosome, {}'),
    ('Paired reads mapped to different chromosomes (MAPQ>=10)', 'Mate diff chrom, MQ>=10'    , None, 'reads', True,  'Number of paired reads, mapped with MAPQ>=10 and with a mate mapped to a different chromosome, {}'),
    ('Reads with indel R1'                                    , 'Reads with indel R1'        , None, 'reads', False, 'Number of R1 reads containing at least 1 indel, {}'),
    ('Reads with indel R2'                                    , 'Reads with indel R2'        , None, 'reads', False, 'Number of R2 reads containing at least 1 indel, {}'),
    # Alignments stats:
    ('Total alignments'                                       , 'Alignments'                 , None, 'reads', True,  'Total number of alignments with MQ > 0, {}'),
    ('Secondary alignments'                                   , 'Second\'ry'                 , '%',  'reads', True,  'Number of secondary alignments, {}. Secondary alignment occurs when '
                                                                                                                        'a given read could align reasonably well to more than one place. '
                                                                                                                        'One of the possible reported alignments is termed "primary" and '
                                                                                                                        'the others will be marked as "secondary".'),
    ('Supplementary (chimeric) alignments'                    , 'Suppl\'ry'                  , None, 'reads', True,  'Number of supplementary (chimeric) alignments, {}. A chimeric read is split '
                                                                                                                        'over multiple loci (possibly due to structural variants). One alignment is '
                                                                                                                        'referred to as the representative alignment, the other are supplementary'),
    # Read length stats:
    ('Estimated read length'                                  , 'Read len'                   , '#',  'len',   False, 'Estimated read length. Total number of input bases divided by the number of reads'),
    ('Insert length: mean'                                    , 'Avg IS'                     , None, 'len',   False, 'Insert length: mean'),
    ('Insert length: median'                                  , 'Med IS'                     , '#',  'len',   False, 'Insert length: median'),
    ('Insert length: standard deviation'                      , 'IS std'                     , None, 'len',   False, 'Insert length: standard deviation'),
    # Coverage:
    ('Average sequenced coverage over genome'                 , 'Cov'                        , '#',  'x',     True,  'Average sequenced coverage over genome'),
    # Bases stats:
    ('Total bases'                                            , 'Bases'                      , '#',  'bases', True,  'Total number of bases sequenced, {}'),
    ('Total bases R1'                                         , 'Bases R1'                   , None, 'bases', False, 'Total number of bases sequenced on R1 reads, {}'),
    ('Total bases R2'                                         , 'Bases R2'                   , None, 'bases', False, 'Total number of bases sequenced on R2 reads, {}'),
    ('Mapped bases R1'                                        , 'Mapped bases R1'            , None, 'bases', False, 'Number of mapped bases on R1 reads, {}'),
    ('Mapped bases R2'                                        , 'Mapped bases R2'            , None, 'bases', False, 'Number of mapped bases on R2 reads, {}'),
    ('Soft-clipped bases R1'                                  , 'Soft-clip bases R1'         , None, 'bases', False, 'Number of soft-clipped bases on R1 reads, {}'),
    ('Soft-clipped bases R2'                                  , 'Soft-clip bases R2'         , None, 'bases', False, 'Number of soft-clipped bases on R2 reads, {}'),
    ('Mismatched bases R1'                                    , 'MM bases R1'                , None, 'bases', False, 'Number of mismatched bases on R1, {}, which is the sum of SNP count and indel lengths. '
                                                                                                                        'It does not count anything within soft clipping, or RNA introns. '
                                                                                                                        'It also does not count a mismatch if either the reference base or read base is N'),
    ('Mismatched bases R2'                                    , 'MM bases R2'                , None, 'bases', False, 'Number of mismatched bases on R2, {}, which is the sum of SNP count and indel lengths. '
                                                                                                                        'It does not count anything within soft clipping, or RNA introns. '
                                                                                                                        'It also does not count a mismatch if either the reference base or read base is N'),
    ('Mismatched bases R1 (excl. indels)'                     , 'MM bases R1 excl indels'    , None, 'bases', False, 'Number of mismatched bases on R1, {}. The indels lengts are ignored. '
                                                                                                                        'It does not count anything within soft clipping, or RNA introns. '
                                                                                                                        'It also does not count a mismatch if either the reference base or read base is N'),
    ('Mismatched bases R2 (excl. indels)'                     , 'MM bases R2 excl indels'    , None, 'bases', False, 'Number of mismatched bases on R2, {}. The indels lengts are ignored. '
                                                                                                                        'It does not count anything within soft clipping, or RNA introns. '
                                                                                                                        'It also does not count a mismatch if either the reference base or read base is N'),
    ('Q30 bases'                                              , 'Q30'                        , '%',  'bases', True,  'Number of bases with BQ >= 30, {}'),
    ('Q30 bases R1'                                           , 'Q30 R1'                     , None, 'bases', False, 'Number of bases on R1 reads with BQ >= 30, {}'),
    ('Q30 bases R2'                                           , 'Q30 R2'                     , None, 'bases', False, 'Number of bases on R2 reads with BQ >= 30, {}'),
    ('Q30 bases (excl. dups & clipped bases)'                 , 'Q30 excl dup & clipped'     , None, 'bases', True,  'Number of non-clipped bases with BQ >= 30 on non-duplicate reads, {}'),
    # General metrics. Showing only when general metrics are different for different samples, otherwise showing in the header
    ('Bases in reference genome'                              , 'Bases in ref. genome'       , '#',  'bases', False, 'Bases in reference genome'              ),
    ('Bases in target bed [% of genome]'                      , 'Bases in target bed'        , '#',  '%'    , False, 'Bases in target bed [% of genome]'      ),
    ('Provided sex chromosome ploidy'                         , 'Provided sex chrom ploidy'  , None,  None  , False, 'Provided sex chromosome ploidy'         ),
    ('DRAGEN mapping rate [mil. reads/second]'                , 'DRAGEN map rate'            , None,  None  , False, 'DRAGEN mapping rate [mil. reads/second]'),
]

GENERAL_METRICS = [
    # id_in_data                              # title                            # format                         # modify
    ('Bases in reference genome'              , 'Bases in ref. genome:'           , base_format                    , lambda v: v * config.base_count_multiplier ),
    ('Bases in target bed [% of genome]'      , 'Bases in target bed:'            , '{} % of genome'               , None                                       ),
    ('Provided sex chromosome ploidy'         , 'Prov. sex chrom ploidy:'         , '{}'                           , None                                       ),
    ('DRAGEN mapping rate [mil. reads/second]', 'DRAGEN mapping rate:'            , '{:,.2f} [mil. reads/second]'  , None                                       ),
]

def make_mapping_stats_headers(metric_names):
    # Init general stats table
    stats_headers = OrderedDict()

    # Init beeswarm plot
    beeswarm_keys = OrderedDict()

    for id_in_data, title, showing, unit, show_in_beeswarm, descr in MAPPING_METRICS:
        col = dict(
            title=title,
            description=descr,
            min=0,
        )
        if unit == 'reads':
            col['scale'] = 'RdYlGn'
        if unit == 'bases':
            col['scale'] = 'RdBu'
        if unit == 'len':
            col['scale'] = 'BrBG'
        if unit == 'x':
            col['scale'] = 'PiYG'

        if id_in_data + ' pct' in metric_names:
            # if % value is available, showing it instead of the number value; the number value will be hidden
            pct_col = dict(
                col,
                description=descr.replace(', {}', '').replace('Number of ', '% of '),
                max=100,
                suffix='%',
                hidden=showing != '%'
            )
            stats_headers[id_in_data + ' pct'] = pct_col

        col['hidden'] = showing != '#'
        if unit == 'reads':
            col['description'] = col['description'].format(config.read_count_desc)
            col['modify'] = lambda x: x * config.read_count_multiplier
            col['shared_key'] = 'read_count'
            col['format'] = read_format
        if unit == 'bases':
            col['description'] = col['description'].format(config.base_count_desc)
            col['modify'] = lambda x: x * config.base_count_multiplier
            col['shared_key'] = 'base_count'
            col['format'] = base_format
        if unit == 'len':
            col['suffix'] = ' bp'
            col['format'] = '{:,.0f}'
        if unit == 'x':
            col['suffix'] = ' x'
            col['format'] = '{:,.1f}'
        if unit == '%':
            col['suffix'] = ' %'
            col['format'] = '{:,.1f}'

        stats_headers[id_in_data] = col

        if show_in_beeswarm:
            suffix = ''
            if unit == 'reads':
                suffix = ' ' + config.read_count_prefix
            if unit == 'bases':
                suffix = ' ' + config.base_count_prefix
            beeswarm_keys[id_in_data] = dict(col,
                suffix=suffix
            )

    return stats_headers, beeswarm_keys






























