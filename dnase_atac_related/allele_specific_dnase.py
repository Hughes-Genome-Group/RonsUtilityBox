"""
Extract DNase/ATAC reads over specific SNPs, filter them and test for
significantly allele specific signals
"""

# import stuff
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import pysam
import sys
import argparse
import re

from os.path import isfile  # import file checker
from scipy.stats import binom_test
import statsmodels.stats.multitest as smm

# Custom functions -------------------------------------------------------------
def isNumber(s):
    try:
        float(s)
        return True
    except ValueError:
        return False

# Define arguments and description ---------------------------------------------
parser = argparse.ArgumentParser(description='Calculate DNase-seq/ATAC-seq allelic biases from reads over given SNPs.')
parser.add_argument('-s', '--snps',
                    metavar='<snp.file>',
                    type=str,
                    nargs=1,
                    required=True,
                    help='SNP file. Bed-like format with mininimum: chr start end id reference alternative. If more columns supplied specify in which columns to find the reference and alternative base using --refcol and --altcol.')

parser.add_argument('-b', '--bam',
                    metavar='<bam.file1>',
                    nargs='+',
                    required=True,
                    help='Space separated list of bam files. Use same individual and compatible source!')

parser.add_argument('--max_missmatches',
                    metavar='M',
                    type=int,
                    nargs=1,
                    default=2,
                    help='Number of missmatches per read allowed. Reads with more missmatches will be filtered. Default: 2')

parser.add_argument('--min_mapq',
                    metavar='Q',
                    type=int,
                    nargs=1,
                    default=0,
                    help='Minimum mapping quality at the SNP position for a read to be considered. Default: 0')

parser.add_argument('--min_reads',
                    metavar='R',
                    type=int,
                    nargs=1,
                    default=10,
                    help='Minimum number of valid reads per SNP required to test it for allelic imbalance. Default: 10')

parser.add_argument('-f', '--format',
                    metavar='bed',
                    type=str,
                    nargs=1,
                    default='bed',
                    choices=('bed', 'vcf'),
                    help='Format of the snp file. [bed or vcf] with default: bed')

parser.add_argument('--refcol',
                    metavar='N',
                    type=int,
                    nargs=1,
                    help='Column where to find the reference base. Default for VCF format is 4 and for bed format 5.')

parser.add_argument('--altcol',
                    metavar='N',
                    type=int,
                    nargs=1,
                    help='Column where to find the alternative base. Default for VCF format is 5 and for bed format 6.')

parser.add_argument('--sortby',
                    metavar='<pvalue/position>',
                    type=str,
                    nargs=1,
                    default='pvalue',
                    help='Select if to sort the output by pvalue or chromosomal position. [Default: pvalue]')


# Parse, Test and Assign Arguments ---------------------------------------------
args = parser.parse_args()

# Test and assign arguments
input_snp_file = args.snps[0]  # input a snp_file in bed (vcf) format
if not isfile(input_snp_file):
    print('The provided SNP file [-s --snps] is not a valid file. Please make sure to supply a valid file and check -h!')
    sys.exit()

# test all supplied bam files
for bam_file in args.bam:
    if not isfile(bam_file):
        print(bam_file + ' is not a valid file please check your input!')
        sys.exit()

# Filter Parameters
SNP_FORMAT = args.format
MAX_MISSMATCHES_PER_READ = args.max_missmatches
MIN_MAPPING_QUAL_AT_SNP = args.min_mapq
MIN_READS_AT_SNP = args.min_reads

# Ref and Alt column adjustments.
ref_col = 0
alt_col = 0
if args.refcol is not None:
    ref_col = args.refcol
else:
    if args.format == 'bed':
        ref_col = 5
    elif args.format == 'vcf':
        ref_col = 4

if args.altcol is not None:
    alt_col = args.altcol
else:
    if args.format == 'bed':
        alt_col = 6
    elif args.format == 'vcf':
        alt_col = 5
# adjust for 0-based indexing
ref_col -= 1
alt_col -= 1


################################################################################
####                                START                                  #####
################################################################################

snp_dict = {}   # intialise an empty dict to store snp info per ID

# init some count variables for run stats
indel_count = 0
not_queried_count = 0  # count for non tested SNPs

# 1) Get & format SNP input as dict --------------------------------------------
# read input save SNP positions in snp_dict
with open(input_snp_file, "r") as sfh:

    # check file format [PLACEHOLDER]

    for line in sfh:

        if re.match('^#', line):  # skip comment and header lines
            continue
        line_split = line.split()
        # skip indels
        if len(line_split[ref_col]) != 1 | len(line_split[alt_col]) != 1:
            indel_count += 1
            continue

        # save chr,pos,id(name) according to snp file format
        if SNP_FORMAT == 'bed':
            snp_dict[line_split[3]] = {
                'chr': line_split[0],
                'pos': int(line_split[1]), #save as 0-based coord
                'id': line_split[3],
                'ref': line_split[ref_col],
                'alt': line_split[alt_col]
            }
        elif SNP_FORMAT == 'vcf':
            snp_dict[line_split[3]] = {
                'chr': 'chr' + line_split[0],
                'pos': int(line_split[1]) - 1, #save as 0-based coord
                'id': line_split[2],
                'ref': line_split[ref_col],
                'alt': line_split[alt_col]
            }

# 2) Extract and filter reads over SNP positions -------------------------------
for input_bamfile in args.bam:

    # open as bam file
    bamfile = pysam.AlignmentFile(input_bamfile, "rb")

    for key in snp_dict:
        # save reads in temp list
        temp_reads = []
        # fetch reads // correct for pysam.fetch needing 1-based coordinate
        for read in bamfile.fetch(snp_dict[key]['chr'], snp_dict[key]['pos']+1, snp_dict[key]['pos']+2):

            # Filter.1) filter for number of mismatches per read
            read_missmatches = read.get_tag('NM')  # get read missmatches from NM tag
            if read_missmatches > MAX_MISSMATCHES_PER_READ:
                continue # skip reads with to many missmatches

            # Filter.2) Filter for Mapping quality at SNP position
            base_mapqs = read.query_alignment_qualities
            temp_pos = snp_dict[key]['pos'] - read.reference_start # get the relative position of the snp for that read while correcting back to 0-based coord
            if base_mapqs[temp_pos] < MIN_MAPPING_QUAL_AT_SNP:
                continue  # skip reads with poor mapq at SNP position

            # append filter passing reads to temp_list
            temp_reads.append(read)

        # add all valid read to the snp_dict read slot
        if 'reads' in snp_dict[key]:
            snp_dict[key]['reads'] = snp_dict[key]['reads'] + list(temp_reads)
        else:
            snp_dict[key]['reads'] = list(temp_reads)

    # close up bam file
    bamfile.close()

# Filter.3) Once through all bam files. Go through every SNP and check if
# the minimum read number has been reached ... fag and discard otherwise
for key in snp_dict:
    if len(snp_dict[key]['reads']) < MIN_READS_AT_SNP:
        snp_dict[key]['flag_sufficient_reads'] = False
        snp_dict[key]['reads'] = []  #delete non sufficient reads
    else:
        # flag that sufficient reads have been found
        snp_dict[key]['flag_sufficient_reads'] = True


# 3) Split allelic reads -------------------------------------------------------
# Go through all reads and split bases at SNP positons into allels if sufficent reads were found

# for every SNP in snp_dict
for key in snp_dict:

    # init report strings
    snp_dict[key]['report_comment'] = ''  # init a comment tag per snp
    snp_dict[key]['report_ref'] = ''
    snp_dict[key]['report_alt'] = ''
    snp_dict[key]['report_other'] = ''

    # check for suffient reads: proceed if OK
    if snp_dict[key]['flag_sufficient_reads'] == False:
        not_queried_count += 1
        snp_dict[key]['report_ref'] = '.:.'
        snp_dict[key]['report_alt'] = '.:.:.'
        snp_dict[key]['report_comment'] = 'Insufficient_Total_Reads;'
        snp_dict[key]['report_other'] = '.:.:.'
        continue # skip the rest for this SNP
    else:
        # create a nested dictonary storing all occuring bases
        snp_dict[key]['allelic_dict'] = {}
        # check base at SNP pos and assign reads to alleles
        for i in range(len(snp_dict[key]['reads'])):
            read_start = snp_dict[key]['reads'][i].reference_start
            read_seq = snp_dict[key]['reads'][i].query_sequence
            pos_snp = snp_dict[key]['pos'] - read_start # get the relative position of the snp for that read while correcting back to 0-based coord
            base_snp = read_seq[pos_snp] #get snp position base
            # if base already found in one of the reads > count up
            if base_snp in snp_dict[key]['allelic_dict']:
                snp_dict[key]['allelic_dict'][base_snp]['count'] = snp_dict[key]['allelic_dict'][base_snp]['count'] + 1
            # else set novel base occuring
            else:
                snp_dict[key]['allelic_dict'][base_snp] = {'count': 1}

    # 5) Test fo allelic imbalance -------------------------------------------------
        ref_allele_count = 0  # initialise temp variables
        ref_allele_base = snp_dict[key]['ref']  # base to calc the binom test relative to
        # check if reference genome base is present in the reads counts
        if snp_dict[key]['ref'] in snp_dict[key]['allelic_dict']:
            # if so use the reference count as base for the testing
            ref_allele_count = snp_dict[key]['allelic_dict'][snp_dict[key]['ref']]['count']
            # make a report for the reference base (Base and counts)
            snp_dict[key]['report_ref'] = snp_dict[key]['ref'] + ':' + str(ref_allele_count)
        else:
            # else use the base with most counts
            for allele_bases in snp_dict[key]['allelic_dict']:
                if allele_bases != snp_dict[key]['alt'] and snp_dict[key]['allelic_dict'][allele_bases]['count'] > ref_allele_count:
                    ref_allele_count = snp_dict[key]['allelic_dict'][allele_bases]['count']
                    ref_allele_base = allele_bases
            # make a report for the reference base (Base and counts)
            snp_dict[key]['report_ref'] = snp_dict[key]['ref'] + ':0'
            if ref_allele_base != snp_dict[key]['ref']:  # found an substitute ref base to use
                snp_dict[key]['report_ref'] = snp_dict[key]['report_ref'] + ';' + ref_allele_base + ':' + str(ref_allele_count)
            # and flag that no reads for the designated reference base were found
            snp_dict[key]['report_comment'] = snp_dict[key]['report_comment'] + 'No_Ref_Reads;Calc_rel_to_' + ref_allele_base + ';'

        # check if no alternative reference base could be found
        # set the comment tag and output strings accordingly
        if ref_allele_base == snp_dict[key]['ref'] and not snp_dict[key]['ref'] in snp_dict[key]['allelic_dict']:
            if snp_dict[key]['alt'] in snp_dict[key]['allelic_dict']:
                not_queried_count += 1
                snp_dict[key]['report_alt'] = snp_dict[key]['alt'] + ':' + str(snp_dict[key]['allelic_dict'][snp_dict[key]['alt']]['count']) + ':.'
                snp_dict[key]['report_comment'] = snp_dict[key]['report_comment'] + 'Only_Alt_Reads;'
            else:
                not_queried_count += 1
                snp_dict[key]['report_alt'] = '.:.:.'
                snp_dict[key]['report_comment'] = snp_dict[key]['report_comment'] + 'No_Ref_nor_Alt_Reads;'
            snp_dict[key]['report_other'] = '.:.:.'
        else:
            # test for allelic imbalance with every present base relative to the ref base
            # check if only one none zero base is present and skip and report if so
            if len(snp_dict[key]['allelic_dict']) <= 1:
                not_queried_count += 1
                snp_dict[key]['report_ref'] = '.:.'
                snp_dict[key]['report_alt'] = '.:.:.'
                snp_dict[key]['report_comment'] = 'Insufficient_Alleles;'
            else:
                # test for allelic imbalance for every present base
                for test_base in snp_dict[key]['allelic_dict']:
                    if test_base == ref_allele_base:  # skip ref base
                        continue
                    # run binomial test: count of test base vs. test base + ref base count
                    snp_dict[key]['allelic_dict'][test_base]['pvalue'] = binom_test(snp_dict[key]['allelic_dict'][test_base]['count'], (snp_dict[key]['allelic_dict'][test_base]['count'] + ref_allele_count))

    # check if reads for designated variant have been found
    # create a base:count:pvalue string
    if not snp_dict[key]['alt'] in snp_dict[key]['allelic_dict']:
        not_queried_count += 1
        snp_dict[key]['report_comment'] = snp_dict[key]['report_comment'] + 'No_Alt_Reads;'
        snp_dict[key]['report_alt'] = snp_dict[key]['alt'] + ':0:.'
    else:
        snp_dict[key]['report_alt'] = snp_dict[key]['alt'] + ':' + str(snp_dict[key]['allelic_dict'][snp_dict[key]['alt']]['count']) + ':' + str(snp_dict[key]['allelic_dict'][snp_dict[key]['alt']]['pvalue'])

    # assemble other variants test outputs (not refbase and not alt base and not reassigned ref base)
    for test_base in snp_dict[key]['allelic_dict']:
        if test_base != ref_allele_base and test_base != snp_dict[key]['alt']:
            snp_dict[key]['report_other'] = snp_dict[key]['report_other'] + test_base + ':' + str(snp_dict[key]['allelic_dict'][test_base]['count']) + ':' + str(snp_dict[key]['allelic_dict'][test_base]['pvalue']) + ';'

    # Delete Dict reads entries for memory release
    del snp_dict[key]['reads']

# 6) FDR -----------------------------------------------------------------------
# apply FDR correction only to the pvalues of SNPS matching the referene and alternative bases
# 6.1) Fetch all valid/queried SNPs and pvalues
valid_tests_dict = {}
for key in snp_dict:
    # dont use uf insufficient reads, pvalue is not a valid number ref base is
    # not the designated ref base and no other weird stuff in comment section happening
    if snp_dict[key]['flag_sufficient_reads'] and isNumber(snp_dict[key]['allelic_dict'][snp_dict[key]['alt']]['pvalue']) and snp_dict[key]['allelic_dict'][snp_dict[key]['alt']]['count'] > 0 and snp_dict[key]['report_comment'] == '':
        valid_tests_dict.update({key: {'pvalue': snp_dict[key]['allelic_dict'][snp_dict[key]['alt']]['pvalue']}})

# 6.2) Perform FDR
fdr_pvalues = []
fdr_keys = []
for key in valid_tests_dict:
    fdr_keys.append(key)  # save key
    # get all valid/passing p-values in list and apply BH FDR calc
    fdr_pvalues.append(valid_tests_dict[key]['pvalue'])

fdr_qvalues = smm.multipletests(fdr_pvalues, alpha=0.05, method='fdr_bh')[1] # calc FDR_BH

# foreach key get the adjusted pvalue (qvalue)
for i in range(len(fdr_keys)):
    valid_tests_dict[fdr_keys[i]]['qvalue'] = fdr_qvalues[i]

# Report What got tested
total_count = len(snp_dict)
queried_count = total_count - not_queried_count
valid_pvalue_count = len(valid_tests_dict)  # get number of valid pvalues
print('# Total SNPs: %s  Queried: %s  Not Queried: %s' % (total_count, queried_count, not_queried_count))

# 7) Assemble and Print Output -------------------------------------------------
# SORT
# Order keys according to desired output:
sorted_keys = []
sort_keys = []
sort_values = []

if args.sortby[0] == 'pvalue':
    # get keys p values from snp_dict
    for k in snp_dict:
        sort_keys.append(k)
        if k in valid_tests_dict:
            sort_values.append(valid_tests_dict[k]['pvalue'])
        else:
            sort_values.append(1)
    # sort
    sorted_keys = [x for (y,x) in sorted(zip(sort_values, sort_keys))]

elif args.sortby[0] == 'position':
    # get chr and pos arguments, trim the chr from chr prior to sorting
    for k in snp_dict:
        temp_chr = snp_dict[k]['chr']
        if 'hr' in temp_chr: # strip chr
            temp_chr = re.sub('[c,C]*hr', '', temp_chr)
        if temp_chr == 'X':  #replace non decimal characters with appro. int
            temp_chr = 23
        elif temp_chr == 'Y':
            temp_chr = 24
        elif temp_chr == 'M':
            temp_chr = 25
        sort_values.append((int(temp_chr), snp_dict[k]['pos'], k))  # add chr position tuple
    # sort by chr and position
    sort_values.sort(
        key = lambda l: (l[0], l[1])
    )
    for s in sort_values:
        sorted_keys.append(s[2])

# PRINT
for key in sorted_keys:
    # produce output string
    # bed format
    output = snp_dict[key]['chr'] + "\t" + str(snp_dict[key]['pos']) + "\t" + str(snp_dict[key]['pos'] + 1) + "\t" + snp_dict[key]['id'] + "\t" + snp_dict[key]['ref'] + "\t" + snp_dict[key]['alt']
    # add pvalue and qvalue if valid ones were calculated (for matching ref and alt)
    if key in valid_tests_dict:
        output += "\t" + str(valid_tests_dict[key]['pvalue']) + "\t" + str(valid_tests_dict[key]['qvalue'])
    else:  # add . as not available
        output += "\t.\t."
    # add detailed base count and test values
    output += "\t" + snp_dict[key]['report_ref'] + ':' + snp_dict[key]['report_alt']
    if snp_dict[key]['report_other'] != '':
        output += ':' + snp_dict[key]['report_other']
    output += "\t"
    # add comment or . were appropriate
    if snp_dict[key]['report_comment'] == '':
        output += '.'
    else:
        output += snp_dict[key]['report_comment']

    # PRINT
    print(output)