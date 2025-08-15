#!/usr/bin/env python3
"""
Copyright 2017 Ryan Wick (rrwick@gmail.com)
https://github.com/rrwick/Unicycler

This module contains the main script for the Unicycler assembler. It is executed when a user runs
`unicycler` (after installation) or `unicycler-runner.py`.

This file is part of Unicycler. Unicycler is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by the Free Software Foundation,
either version 3 of the License, or (at your option) any later version. Unicycler is distributed in
the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
details. You should have received a copy of the GNU General Public License along with Unicycler. If
not, see <http://www.gnu.org/licenses/>.
"""

import argparse
import os
import sys
import shutil
import random
import itertools
import multiprocessing
from .alignment import AlignmentScoringScheme
from .assembly_graph import AssemblyGraph
from .assembly_graph_copy_depth import determine_copy_depth
from .bridge_long_read_simple import create_simple_long_read_bridges
from .miniasm_assembly import make_miniasm_string_graph
from .bridge_miniasm import create_miniasm_bridges
from .bridge_long_read import create_long_read_bridges
from .bridge_spades_contig import create_spades_contig_bridges
from .bridge_loop_unroll import create_loop_unrolling_bridges
from .misc import int_to_str, float_to_str, quit_with_error, get_percentile, bold, \
    check_input_files, MyHelpFormatter, print_table, get_ascii_art, \
    get_default_thread_count, spades_path_and_version, makeblastdb_path_and_version, \
    tblastn_path_and_version, racon_path_and_version, gfa_path, red
from .spades_func import get_best_spades_graph
from .blast_func import find_start_gene, CannotFindStart
from .unicycler_align import fix_up_arguments, semi_global_align_long_reads, load_references, \
    load_sam_alignments, print_alignment_summary_table
from .read_ref import get_read_nickname_dict, load_long_reads
from . import log
from . import settings
from .version import __version__


def main():
    """
    Script execution starts here.
    """
    random.seed(0)  # Fixed seed so the program produces the same output every time it's run.

    full_command = ' '.join(('"' + x + '"' if ' ' in x else x) for x in sys.argv)
    args = get_arguments()
    out_dir_message = make_output_directory(args.out, args.verbosity)
    short_reads_available = bool(args.short1) or bool(args.unpaired)
    long_reads_available = bool(args.long)

    check_input_files(args)
    print_intro_message(args, full_command, out_dir_message)
    spades_version = check_dependencies(args, short_reads_available, long_reads_available)

    counter = itertools.count(start=1)  # Files are numbered in chronological order.
    bridges = []

    if short_reads_available:
        # Produce a SPAdes assembly graph with a k-mer that balances contig length and connectivity.
        spades_graph_prefix = gfa_path(args.out, next(counter), 'spades_graph')[:-4]
        best_spades_graph = gfa_path(args.out, next(counter), 'depth_filter')
        if os.path.isfile(best_spades_graph):
            log.log('\nSPAdes graph already exists. Will use this graph instead of running '
                    'SPAdes:\n  ' + best_spades_graph)
            graph = AssemblyGraph(best_spades_graph, None)
        else:
            graph = get_best_spades_graph(args.short1, args.short2, args.unpaired, args.out,
                                          args.depth_filter, args.verbosity,
                                          args.spades_path, args.threads, args.keep,
                                          args.kmer_count, args.min_kmer_frac, args.max_kmer_frac,
                                          args.kmers, args.linear_seqs, args.largest_component,
                                          spades_graph_prefix, args.spades_options, spades_version)
        determine_copy_depth(graph)
        if args.keep > 0 and not os.path.isfile(best_spades_graph):
            graph.save_to_gfa(best_spades_graph, save_copy_depth_info=True, newline=True,
                              include_insert_size=True)

        clean_up_spades_graph(graph)
        if args.keep > 0:
            overlap_removed_graph_filename = gfa_path(args.out, next(counter), 'overlaps_removed')
            graph.save_to_gfa(overlap_removed_graph_filename, save_copy_depth_info=True,
                              newline=True, include_insert_size=True)

        anchor_segments = get_anchor_segments(graph, args.min_anchor_seg_len)

        # Make an initial set of bridges using the SPAdes contig paths. This step is skipped when
        # using conservative bridging mode (in that case we don't trust SPAdes contig paths at all).
        if args.mode != 0:
            bridges += create_spades_contig_bridges(graph, anchor_segments)
            bridges += create_loop_unrolling_bridges(graph, anchor_segments)
            if not bridges:
                log.log('none found', 1)

        graph.paths = {}  # Now that we've made short read bridges, we no longer need the paths.

    else:  # short reads not available
        graph = None
        anchor_segments = []

    scoring_scheme = AlignmentScoringScheme(args.scores)

    if long_reads_available:
        read_dict, read_names, long_read_filename = load_long_reads(args.long, output_dir=args.out)
        read_nicknames = get_read_nickname_dict(read_names)
    else:
        read_dict, read_names, long_read_filename, read_nicknames = {}, [], '', {}

    if long_reads_available and not args.no_miniasm:
        string_graph = make_miniasm_string_graph(graph, read_dict, long_read_filename,
                                                 scoring_scheme, read_nicknames, counter, args,
                                                 anchor_segments, args.existing_long_read_assembly)
    else:
        string_graph = None

    if not short_reads_available and string_graph is None:
        quit_with_error('miniasm assembly failed')

    if short_reads_available and long_reads_available:
        if string_graph is not None and not args.no_miniasm:
            bridges += create_miniasm_bridges(graph, string_graph, anchor_segments,
                                              scoring_scheme, args.verbosity, args.min_bridge_qual)

        if not args.no_simple_bridges:
            bridges += create_simple_long_read_bridges(graph, args.out, args.keep, args.threads,
                                                       read_dict, long_read_filename,
                                                       scoring_scheme, anchor_segments)
        if not args.no_long_read_alignment:
            read_names, min_scaled_score, min_alignment_length = \
                align_long_reads_to_assembly_graph(graph, anchor_segments, args, full_command,
                                                   read_dict, read_names, long_read_filename)

            expected_linear_seqs = args.linear_seqs > 0
            bridges += create_long_read_bridges(graph, read_dict, read_names, anchor_segments,
                                                args.verbosity, min_scaled_score, args.threads,
                                                scoring_scheme, min_alignment_length,
                                                expected_linear_seqs, args.min_bridge_qual)

    if short_reads_available:
        seg_nums_used_in_bridges = graph.apply_bridges(bridges, args.verbosity,
                                                       args.min_bridge_qual)
        if args.keep > 0:
            graph.save_to_gfa(gfa_path(args.out, next(counter), 'bridges_applied'),
                              save_seg_type_info=True, save_copy_depth_info=True, newline=True)

        graph.clean_up_after_bridging_1(anchor_segments, seg_nums_used_in_bridges)
        graph.clean_up_after_bridging_2(seg_nums_used_in_bridges, args.min_component_size,
                                        args.min_dead_end_size, graph, anchor_segments)
        if args.keep > 2:
            log.log('', 2)
            graph.save_to_gfa(gfa_path(args.out, next(counter), 'cleaned'),
                              save_seg_type_info=True, save_copy_depth_info=True)
        graph.merge_all_possible(anchor_segments, args.mode)
        if args.keep > 2:
            graph.save_to_gfa(gfa_path(args.out, next(counter), 'merged'))

        log.log_section_header('Bridged assembly graph')
        log.log_explanation('The assembly is now mostly finished and no more structural changes '
                            'will be made. Ideally the assembly graph should now have one contig '
                            'per replicon and no erroneous contigs (i.e. a complete assembly). '
                            'If there are more contigs, then the assembly is not complete.',
                            verbosity=1)
        graph.final_clean()
        if args.keep > 0:
            graph.save_to_gfa(gfa_path(args.out, next(counter), 'final_clean'))
        log.log('')
        graph.print_component_table()

    else:  # only long reads available
        graph = string_graph

    if not args.no_rotate:
        rotate_completed_replicons(graph, args, counter)

    log.log_section_header('Assembly complete')
    final_assembly_fasta = os.path.join(args.out, 'assembly.fasta')
    final_assembly_gfa = os.path.join(args.out, 'assembly.gfa')
    graph.save_to_gfa(final_assembly_gfa)
    graph.save_to_fasta(final_assembly_fasta, min_length=args.min_fasta_length)

    log.log('')


def get_arguments():
    """
    Parse the command line arguments.
    """
    description = bold('Unicycler: an assembly pipeline for bacterial genomes')
    this_script_dir = os.path.dirname(os.path.realpath(__file__))

    if '--helpall' in sys.argv or '--allhelp' in sys.argv or '--all_help' in sys.argv:
        sys.argv.append('--help_all')
    show_all_args = '--help_all' in sys.argv

    # Show the ASCII art if the terminal is wide enough for it.
    terminal_width = shutil.get_terminal_size().columns
    if terminal_width >= 70:
        full_description = 'R|' + get_ascii_art() + '\n\n' + description
    else:
        full_description = description
    parser = argparse.ArgumentParser(description=full_description, formatter_class=MyHelpFormatter,
                                     add_help=False)

    help_group = parser.add_argument_group('Help')
    help_group.add_argument('-h', '--help', action='help',
                            help='Show this help message and exit')
    help_group.add_argument('--help_all', action='help',
                            help='Show a help message with all program options')
    help_group.add_argument('--version', action='version', version='Unicycler v' + __version__,
                            help="Show Unicycler's version number")

    input_group = parser.add_argument_group('Input')
    input_group.add_argument('-1', '--short1', required=False,
                             help='FASTQ file of first short reads in each pair')
    input_group.add_argument('-2', '--short2', required=False,
                             help='FASTQ file of second short reads in each pair')
    input_group.add_argument('-s', '--unpaired', required=False,
                             help='FASTQ file of unpaired short reads')
    input_group.add_argument('-l', '--long', required=False,
                             help='FASTQ or FASTA file of long reads')

    output_group = parser.add_argument_group('Output')
    output_group.add_argument('-o', '--out', required=True,
                              help='Output directory (required)')
    output_group.add_argument('--verbosity', type=int, required=False, default=1,
                              help='R|Level of stdout and log file information (default: 1)\n  '
                                   '0 = no stdout, 1 = basic progress indicators, '
                                   '2 = extra info, 3 = debugging info')
    output_group.add_argument('--min_fasta_length', type=int, required=False, default=100,
                              help='Exclude contigs from the FASTA file which are shorter than '
                                   'this length')
    output_group.add_argument('--keep', type=int, default=1,
                              help='R|Level of file retention (default: 1)\n  '
                                   '0 = only keep final files: assembly (FASTA, GFA and log), '
                                   '1 = also save graphs at main checkpoints, '
                                   '2 = also keep SAM (enables fast rerun in different mode), '
                                   '3 = keep all temp files and save all graphs (for debugging)')

    other_group = parser.add_argument_group('Other')
    other_group.add_argument('-t', '--threads', type=int, required=False,
                             default=get_default_thread_count(),
                             help='Number of threads used')
    other_group.add_argument('--mode', choices=['conservative', 'normal', 'bold'], default='normal',
                             help='B|Bridging mode (default: normal)\n'
                                  '  conservative = smaller contigs, lowest misassembly rate\n'
                                  '  normal = moderate contig size and misassembly rate\n'
                                  '  bold = longest contigs, higher misassembly rate')
    other_group.add_argument('--min_bridge_qual', type=float,
                             help='R|Do not apply bridges with a quality below this value\n'
                                  '  conservative mode default: ' +
                                  str(settings.CONSERVATIVE_MIN_BRIDGE_QUAL) + '\n'
                                  '  normal mode default: ' +
                                  str(settings.NORMAL_MIN_BRIDGE_QUAL) + '\n'
                                  '  bold mode default: ' +
                                  str(settings.BOLD_MIN_BRIDGE_QUAL)
                                  if show_all_args else argparse.SUPPRESS)
    other_group.add_argument('--linear_seqs', type=int, required=False, default=0,
                             help='The expected number of linear (i.e. non-circular) sequences in '
                                  'the underlying sequence')
    other_group.add_argument('--min_anchor_seg_len', type=int, required=False,
                             help='If set, Unicycler will not use segments shorter than this as '
                                  'scaffolding anchors (default: automatic threshold)'
                                  if show_all_args else argparse.SUPPRESS)

    spades_group = parser.add_argument_group('SPAdes assembly',
                                             'These options control the short-read SPAdes '
                                             'assembly at the beginning of the Unicycler pipeline.'
                                             if show_all_args else argparse.SUPPRESS)
    spades_group.add_argument('--spades_path', type=str, default='spades.py',
                              help='Path to the SPAdes executable'
                                   if show_all_args else argparse.SUPPRESS)
    spades_group.add_argument('--min_kmer_frac', type=float, default=0.2,
                              help='Lowest k-mer size for SPAdes assembly, expressed as a '
                                   'fraction of the read length'
                                   if show_all_args else argparse.SUPPRESS)
    spades_group.add_argument('--max_kmer_frac', type=float, default=0.95,
                              help='Highest k-mer size for SPAdes assembly, expressed as a '
                                   'fraction of the read length'
                                   if show_all_args else argparse.SUPPRESS)
    spades_group.add_argument('--kmers', type=str, default=None,
                              help='Exact k-mers to use for SPAdes assembly, comma-separated '
                                   '(example: 21,51,71, default: automatic)'
                                   if show_all_args else argparse.SUPPRESS)
    spades_group.add_argument('--kmer_count', type=int, default=8,
                              help='Number of k-mer steps to use in SPAdes assembly'
                                   if show_all_args else argparse.SUPPRESS)
    spades_group.add_argument('--depth_filter', type=float, default=0.25,
                              help='Filter out contigs lower than this fraction of the chromosomal '
                                   'depth, if doing so does not result in graph dead ends'
                                   if show_all_args else argparse.SUPPRESS)
    spades_group.add_argument('--largest_component', action='store_true',
                              help='Only keep the largest connected component of the assembly '
                                   'graph (default: keep all connected components)'
                                   if show_all_args else argparse.SUPPRESS)
    spades_group.add_argument('--spades_options', type=str, default=None,
                              help='Additional options to be given to SPAdes (example: '
                                   '"--phred-offset 33", default: no additional options)'
                                   if show_all_args else argparse.SUPPRESS)

    miniasm_group = parser.add_argument_group('miniasm+Racon assembly',
                                              'These options control the use of miniasm and Racon '
                                              'to produce long-read bridges.'
                                              if show_all_args else argparse.SUPPRESS)
    miniasm_group.add_argument('--no_miniasm', action='store_true',
                               help='Skip miniasm+Racon bridging (default: use miniasm and Racon '
                                    'to produce long-read bridges)'
                                    if show_all_args else argparse.SUPPRESS)
    miniasm_group.add_argument('--racon_path', type=str, default='racon',
                               help='Path to the Racon executable'
                                    if show_all_args else argparse.SUPPRESS)
    miniasm_group.add_argument('--existing_long_read_assembly', type=str, default=None,
                               help='A pre-prepared long-read assembly for the sample in GFA '
                                    'or FASTA format. If this option is used, Unicycler will skip '
                                    'the miniasm/Racon steps and instead use the given assembly '
                                    '(default: perform long-read assembly using miniasm/Racon)'
                                    if show_all_args else argparse.SUPPRESS)

    long_group = parser.add_argument_group('Long-read alignment and bridging',
                                           'These options control the use of long-read alignment '
                                           'to produce long-read bridges.'
                                           if show_all_args else argparse.SUPPRESS)
    long_group.add_argument('--no_simple_bridges', action='store_true',
                            help='Skip simple long-read bridging (default: use simple '
                                 'long-read bridging)'
                                 if show_all_args else argparse.SUPPRESS)
    long_group.add_argument('--no_long_read_alignment', action='store_true',
                            help='Skip long-read-alignment-based bridging (default: use long-read '
                                 'alignments to produce bridges)'
                                 if show_all_args else argparse.SUPPRESS)
    long_group.add_argument('--contamination', required=False,
                            help='FASTA file of known contamination in long reads'
                            if show_all_args else argparse.SUPPRESS)
    long_group.add_argument('--scores', type=str, required=False, default='3,-6,-5,-2',
                            help='Comma-delimited string of alignment scores: match, mismatch, '
                                 'gap open, gap extend'
                            if show_all_args else argparse.SUPPRESS)
    long_group.add_argument('--low_score', type=float, required=False,
                            help='Score threshold - alignments below this are considered poor '
                                 '(default: set threshold automatically)'
                            if show_all_args else argparse.SUPPRESS)

    cleaning_group = parser.add_argument_group('Graph cleaning',
                                               'These options control the removal of small '
                                               'leftover sequences after bridging is complete.'
                                               if show_all_args else argparse.SUPPRESS)
    cleaning_group.add_argument('--min_component_size', type=int, default=1000,
                                help='Graph components smaller than this size (bp) will be '
                                     'removed from the final graph'
                                     if show_all_args else argparse.SUPPRESS)
    cleaning_group.add_argument('--min_dead_end_size', type=int, default=1000,
                                help='Graph dead ends smaller than this size (bp) will be removed '
                                     'from the final graph'
                                     if show_all_args else argparse.SUPPRESS)

    rotation_group = parser.add_argument_group('Assembly rotation',
                                               'These options control the rotation of completed '
                                               'circular sequence near the end of the Unicycler '
                                               'pipeline.'
                                               if show_all_args else argparse.SUPPRESS)
    rotation_group.add_argument('--no_rotate', action='store_true',
                                help='Do not rotate completed replicons to start at a standard '
                                     'gene (default: completed replicons are rotated)'
                                     if show_all_args else argparse.SUPPRESS)
    rotation_group.add_argument('--start_genes', type=str,
                                default=os.path.join(this_script_dir, 'gene_data',
                                                     'start_genes.fasta'),
                                help='FASTA file of genes for start point of rotated replicons '
                                     '(default: start_genes.fasta)'
                                     if show_all_args else argparse.SUPPRESS)
    rotation_group.add_argument('--start_gene_id', type=float, default=90.0,
                                help='The minimum required BLAST percent identity for a start gene '
                                     'search'
                                     if show_all_args else argparse.SUPPRESS)
    rotation_group.add_argument('--start_gene_cov', type=float, default=95.0,
                                help='The minimum required BLAST percent coverage for a start gene '
                                     'search'
                                     if show_all_args else argparse.SUPPRESS)
    rotation_group.add_argument('--makeblastdb_path', type=str, default='makeblastdb',
                                help='Path to the makeblastdb executable'
                                     if show_all_args else argparse.SUPPRESS)
    rotation_group.add_argument('--tblastn_path', type=str, default='tblastn',
                                help='Path to the tblastn executable'
                                     if show_all_args else argparse.SUPPRESS)

    # If no arguments were used, print the entire help (argparse default is to just give an error
    # like '--out is required').
    if len(sys.argv) == 1:
        parser.print_help(file=sys.stderr)
        sys.exit(1)

    args = parser.parse_args()
    fix_up_arguments(args)

    if (args.short1 and not args.short2) or (args.short2 and not args.short1):
        quit_with_error('you must use both --short1 and --short2 or neither')

    if not args.short1 and not args.short2 and not args.unpaired and not args.long:
        quit_with_error('no input reads provided (--short1, --short2, --unpaired, --long)')

    if not (args.long and (args.short1 or args.unpaired)):  # if not a hybrid assembly
        if args.existing_long_read_assembly:
            quit_with_error('--existing_long_read_assembly requires both short and long read '
                            'inputs')

    if args.keep < 0 or args.keep > 3:
        quit_with_error('--keep must be between 0 and 3 (inclusive)')

    if args.verbosity < 0 or args.verbosity > 3:
        quit_with_error('--verbosity must be between 0 and 3 (inclusive)')

    if args.threads <= 0:
        quit_with_error('--threads must be at least 1')

    if args.kmer_count < 1:
        quit_with_error('--kmer_count must be at least 1')

    if args.kmers is not None:
        args.kmers = args.kmers.split(',')
        try:
            args.kmers = [int(x) for x in args.kmers]
            if any(x % 2 == 0 for x in args.kmers):
                raise ValueError
        except ValueError:
            quit_with_error('--kmers must be comma-separated odd integers without spaces '
                            '(example: --kmers 21,31,41)')
        if any(x > 127 or x < 11 for x in args.kmers):
            quit_with_error('--kmers values must be in the range of 11 to 127 (inclusive)')
        if len(args.kmers) != len(set(args.kmers)):
            quit_with_error('--kmers cannot contain duplicate values')
        args.kmers = sorted(args.kmers)

    # Set up bridging mode related stuff.
    user_set_bridge_qual = args.min_bridge_qual is not None
    if args.mode == 'conservative':
        args.mode = 0
        if not user_set_bridge_qual:
            args.min_bridge_qual = settings.CONSERVATIVE_MIN_BRIDGE_QUAL
    elif args.mode == 'bold':
        args.mode = 2
        if not user_set_bridge_qual:
            args.min_bridge_qual = settings.BOLD_MIN_BRIDGE_QUAL
    else:  # normal
        args.mode = 1
        if not user_set_bridge_qual:
            args.min_bridge_qual = settings.NORMAL_MIN_BRIDGE_QUAL

    # Change some arguments to full paths.
    args.out = os.path.abspath(args.out)
    if args.short1:
        args.short1 = os.path.abspath(args.short1)
    if args.short2:
        args.short2 = os.path.abspath(args.short2)
    if args.unpaired:
        args.unpaired = os.path.abspath(args.unpaired)
    if args.long:
        args.long = os.path.abspath(args.long)

    # Create an initial logger which doesn't have an output file.
    log.logger = log.Log(None, args.verbosity)

    return args


def make_output_directory(out_dir, verbosity):
    """
    Creates the output directory, if it doesn't already exist.
    """
    if not os.path.exists(out_dir):
        try:
            os.makedirs(out_dir)
        except OSError:
            quit_with_error('Unicycler was unable to make the output directory')
        message = 'Making output directory:'
    elif os.listdir(out_dir):
        message = 'The output directory already exists and files may be reused or overwritten:'
    else:  # directory exists but is empty
        message = 'The output directory already exists:'
    message += '\n  ' + out_dir

    # Now that the output directory exists, we can make our logger store all output in a log file.
    log.logger = log.Log(os.path.join(out_dir, 'unicycler.log'), stdout_verbosity_level=verbosity)

    # The message is returned so it can be logged in the 'Starting Unicycler' section.
    return message


def get_anchor_segments(graph, min_anchor_seg_len):
    """
    Returns a list of the graph segments that will be used for bridging.
    """
    log.log('')
    log.log_explanation('Unicycler now selects a set of anchor contigs from the '
                        'single-copy contigs. These are the contigs which will be connected '
                        'via bridges to form the final assembly.', verbosity=1)

    graph_n50 = graph.get_n_segment_length(50.0)
    graph_n80 = graph.get_n_segment_length(80.0)
    graph_n99 = graph.get_n_segment_length(99.0)

    # First we include any segments which are single-copy and not too short.
    anchor_seg_nums = set([x.number for x in graph.get_single_copy_segments()
                           if x.get_length() >= graph_n99 and
                           x.get_length() >= settings.MIN_SINGLE_COPY_LENGTH])

    # Any already-completed segments are included - they don't need bridging but we want to ensure
    # that they are included in the final graph.
    for component in graph.get_connected_components():
        if graph.is_component_complete(component):
            anchor_seg_nums.add(component[0])

    # Next we include any long contigs which didn't have a copy-depth assigned. These are probably
    # single copy and the multiplicity algorithm just failed to call them as such.
    anchor_seg_nums |= set([x.number for x in graph.get_no_copy_depth_segments()
                            if x.get_length() >= graph_n80])

    # We also include any very long contigs, regardless of their copy depth.
    anchor_seg_nums |= set([x.number for x in graph.segments.values()
                            if x.get_length() >= graph_n50])

    # Look for connected components without dead ends which don't have any anchor segments. If
    # there are any, then we should add some anchor segments for that component. This might happen
    # if there are some very tangled small plasmids which don't meet the MIN_SINGLE_COPY_LENGTH
    # threshold. Or if some weird graph connections confounded the multiplicity algorithm.
    connected_components = graph.get_connected_components()
    for component in connected_components:
        dead_ends = sum(graph.dead_end_count(seg) for seg in component)
        anchor_seg_count = sum((1 if seg in anchor_seg_nums else 0) for seg in component)
        if dead_ends > 0 or anchor_seg_count > 0:
            continue

        # First try to get single-copy segments (potentially quite short).
        new_anchor_segs = [seg for seg in component if graph.is_seg_num_single_copy(seg)]

        # If that didn't work, then look for the longest segment in the component which has a
        # single link on at least one end.
        if not new_anchor_segs:
            for seg in sorted(component, key=lambda x: graph.segments[x].get_length(),
                              reverse=True):
                if len(graph.forward_links[seg]) == 1 or len(graph.reverse_links[seg]) == 1:
                    new_anchor_segs = [seg]
                    break

        # Hopefully we found something...
        if new_anchor_segs:
            anchor_seg_nums |= set(new_anchor_segs)

    if min_anchor_seg_len is None:
        min_anchor_seg_len = 0
    anchor_segments = sorted([graph.segments[x] for x in anchor_seg_nums
                              if graph.segments[x].get_length() >= min_anchor_seg_len],
                             reverse=True, key=lambda x: x.get_length())

    log.log('', 2)
    total_anchor_length = sum([x.get_length() for x in anchor_segments])
    log.log(int_to_str(len(anchor_segments)) +
            ' anchor segments (' + int_to_str(total_anchor_length) + ' bp) out of ' +
            int_to_str(len(graph.segments)) +
            ' total segments (' + int_to_str(graph.get_total_length()) + ' bp)')
    log.log('\nAnchor segments:', 2)
    log.log_number_list([x.number for x in anchor_segments], 2)

    return anchor_segments


def sam_references_match(sam_filename, assembly_graph):
    """
    Returns True if the references in the SAM header exactly match the graph segment numbers.
    """
    sam_file = open(sam_filename, 'rt')
    ref_numbers_in_sam = set()
    for line in sam_file:
        if not line.startswith('@'):
            break
        if not line.startswith('@SQ'):
            continue
        line_parts = line.strip().split()
        if len(line_parts) < 2:
            continue
        ref_name_parts = line_parts[1].split(':')
        if len(ref_name_parts) < 2:
            continue
        try:
            ref_numbers_in_sam.add(int(ref_name_parts[1]))
        except ValueError:
            pass

    seg_numbers_in_graph = set(assembly_graph.segments.keys())
    return ref_numbers_in_sam.issubset(seg_numbers_in_graph)


def print_intro_message(args, full_command, out_dir_message):
    """
    Prints a message at the start of the program's execution.
    """
    log.log_section_header('Starting Unicycler', single_newline=True)

    short_reads_available = bool(args.short1)
    long_reads_available = bool(args.long)

    intro_message = 'Welcome to Unicycler, an assembly pipeline for bacterial genomes. '
    if short_reads_available and long_reads_available:
        intro_message += ('Since you provided both short and long reads, Unicycler will perform a '
                          'hybrid assembly. It will first use SPAdes to make a short-read '
                          'assembly graph, and then it will use various methods to scaffold '
                          'that graph with the long reads.')
    elif short_reads_available:
        intro_message += ('Since you provided only short reads, Unicycler will essentially '
                          'function as a SPAdes-optimiser. It will try many k-mer sizes, choose '
                          'the best based on contig length and graph connectivity, and scaffold '
                          'the graph using SPAdes repeat resolution.')
    elif long_reads_available:
        intro_message += ('Since you provided only long reads, Unicycler will assemble the reads '
                          'with miniasm and then run repeated polishing rounds using Racon.')
    log.log_explanation(intro_message, extra_empty_lines_after=0)
    log.log_explanation('For more information, please see https://github.com/rrwick/Unicycler')

    log.log('Command: ' + bold(full_command))
    log.log('')
    log.log('Unicycler version: v' + __version__)
    log.log('Using ' + str(args.threads) + ' thread' + ('' if args.threads == 1 else 's'))
    log.log('')
    if args.threads > 2 * multiprocessing.cpu_count():
        log.log(red('Warning: you have specified a lot more threads than this machine seems to '
                    'have! Was this intentional?'))
        log.log('')
    log.log(out_dir_message)

    if short_reads_available:
        log.log('', 2)
        if args.mode == 0:
            log.log('Bridging mode: conservative', 2)
            if args.min_bridge_qual == settings.CONSERVATIVE_MIN_BRIDGE_QUAL:
                log.log('  using default conservative bridge quality cutoff: ', 2, end='')
            else:
                log.log('  using user-specified bridge quality cutoff: ', 2, end='')
        elif args.mode == 1:
            log.log('Bridging mode: normal', 2)
            if args.min_bridge_qual == settings.NORMAL_MIN_BRIDGE_QUAL:
                log.log('  using default normal bridge quality cutoff: ', 2, end='')
            else:
                log.log('  using user-specified bridge quality cutoff: ', 2, end='')
        else:  # args.mode == 2
            log.log('Bridging mode: bold', 2)
            if args.min_bridge_qual == settings.BOLD_MIN_BRIDGE_QUAL:
                log.log('  using default bold bridge quality cutoff: ', 2, end='')
            else:
                log.log('  using user-specified bridge quality cutoff: ', 2, end='')
        log.log(float_to_str(args.min_bridge_qual, 2), 2)


def check_dependencies(args, short_reads_available, long_reads_available):
    """
    This function prints a table of Unicycler's dependencies and checks their version number.
    It will end the program with an error message if there are any problems.
    """
    log.log('\nDependencies:')
    if args.verbosity <= 1:
        program_table = [['Program', 'Version', 'Status']]
    else:
        program_table = [['Program', 'Version', 'Status', 'Path']]

    if not short_reads_available:
        spades_path, spades_version, spades_status = '', '', 'not used'
    else:
        spades_path, spades_version, spades_status = spades_path_and_version(args.spades_path)
    spades_row = ['spades.py', spades_version, spades_status]
    if args.verbosity > 1:
        spades_row.append(spades_path)
    program_table.append(spades_row)

    # Miniasm/Racon dependencies
    if args.no_miniasm or args.existing_long_read_assembly or not long_reads_available:
        racon_path, racon_version, racon_status = '', '', 'not used'
    else:
        racon_path, racon_version, racon_status = racon_path_and_version(args.racon_path)
    racon_row = ['racon', racon_version, racon_status]
    if args.verbosity > 1:
        racon_row.append(racon_path)
    program_table.append(racon_row)

    # Rotation dependencies
    if args.no_rotate:
        makeblastdb_path, makeblastdb_version, makeblastdb_status = '', '', 'not used'
        tblastn_path, tblastn_version, tblastn_status = '', '', 'not used'
    else:
        makeblastdb_path, makeblastdb_version, makeblastdb_status = \
            makeblastdb_path_and_version(args.makeblastdb_path)
        tblastn_path, tblastn_version, tblastn_status = tblastn_path_and_version(args.tblastn_path)
    makeblastdb_row = ['makeblastdb', makeblastdb_version, makeblastdb_status]
    tblastn_row = ['tblastn', tblastn_version, tblastn_status]
    if args.verbosity > 1:
        makeblastdb_row.append(makeblastdb_path)
        tblastn_row.append(tblastn_path)
    program_table.append(makeblastdb_row)
    program_table.append(tblastn_row)

    row_colours = {}
    for i, row in enumerate(program_table):
        if 'not used' in row:
            row_colours[i] = 'dim'
        elif ('too old' in row or 'too new' in row or 'not found' in row or 'bad' in row or
              'Python problem' in row):
            row_colours[i] = 'red'

    print_table(program_table, alignments='LLLL', row_colour=row_colours, max_col_width=60,
                sub_colour={'good': 'green'})

    quit_if_dependency_problem(spades_status, racon_status, makeblastdb_status, tblastn_status,
                               args)
    return spades_version


def quit_if_dependency_problem(spades_status, racon_status, makeblastdb_status, tblastn_status,
                               args):
    if all(x == 'good' or x == 'not used'
           for x in [spades_status, racon_status, makeblastdb_status, tblastn_status]):
        return

    log.log('')
    if spades_status == 'not found':
        quit_with_error('could not find SPAdes at ' + args.spades_path)
    if spades_status == 'too old' or spades_status == 'too new':
        quit_with_error('Unicycler requires SPAdes v3.14.0 or later')
    if spades_status == 'Python problem':
        quit_with_error('SPAdes cannot run due to an incompatible Python version')
    if spades_status == 'bad':
        quit_with_error('SPAdes was found but does not produce output (make sure to use '
                        '"spades.py" location, not "spades")')
    if makeblastdb_status == 'not found':
        quit_with_error('could not find makeblastdb - either specify its location using '
                        '--makeblastdb_path or use --no_rotate to remove BLAST dependency')
    if tblastn_status == 'not found':
        quit_with_error('could not find tblastn - either specify its location using '
                        '--tblastn_path or use --no_rotate to remove BLAST dependency')
    if racon_status == 'not found':
        quit_with_error('could not find racon - either specify its location using --racon_path '
                        'or use --no_miniasm to remove Racon dependency')
    if racon_status == 'bad':
        quit_with_error('Racon was found but does not produce output')

    # Code should never get here!
    quit_with_error('Unspecified error with Unicycler dependencies')


def rotate_completed_replicons(graph, args, counter):
    completed_replicons = graph.completed_circular_replicons()
    if len(completed_replicons) > 0:
        log.log_section_header('Rotating completed replicons')
        log.log_explanation('Any completed circular contigs (i.e. single contigs which have one '
                            'link connecting end to start) can have their start position changed '
                            'without altering the sequence. For consistency, Unicycler now '
                            'searches for a starting gene (dnaA or repA) in each such contig, and '
                            'if one is found, the contig is rotated to start with that gene on '
                            'the forward strand.')

        rotation_result_table = [['Segment', 'Length', 'Depth', 'Starting gene', 'Position',
                                  'Strand', 'Identity', 'Coverage']]
        blast_dir = os.path.join(args.out, 'blast')
        if not os.path.exists(blast_dir):
            os.makedirs(blast_dir)
        completed_replicons = sorted(completed_replicons, reverse=True,
                                     key=lambda x: graph.segments[x].get_length())
        rotation_count = 0
        for completed_replicon in completed_replicons:
            segment = graph.segments[completed_replicon]
            sequence = segment.forward_sequence

            try:
                seg_name = str(segment.number)
            except AttributeError:
                seg_name = segment.full_name

            log.log('Segment ' + seg_name + ':', 2)
            rotation_result_row = [seg_name, int_to_str(len(sequence)),
                                   float_to_str(segment.depth, 2) + 'x']
            try:
                blast_hit = find_start_gene(sequence, args.start_genes, args.start_gene_id,
                                            args.start_gene_cov, blast_dir, args.makeblastdb_path,
                                            args.tblastn_path)
            except CannotFindStart:
                rotation_result_row += ['none found', '', '', '', '']
            else:
                rotation_result_row += [blast_hit.qseqid, int_to_str(blast_hit.start_pos),
                                        'reverse' if blast_hit.flip else 'forward',
                                        '%.1f' % blast_hit.pident + '%',
                                        '%.1f' % blast_hit.query_cov + '%']
                segment.rotate_sequence(blast_hit.start_pos, blast_hit.flip)
                rotation_count += 1
            rotation_result_table.append(rotation_result_row)

        log.log('', 2)
        print_table(rotation_result_table, alignments='RRRLRLRR', indent=0,
                    sub_colour={'none found': 'red'})
        if rotation_count and args.keep > 0:
            graph.save_to_gfa(gfa_path(args.out, next(counter), 'rotated'), newline=True)
        if args.keep < 3 and os.path.exists(blast_dir):
            shutil.rmtree(blast_dir, ignore_errors=True)


def align_long_reads_to_assembly_graph(graph, anchor_segments, args, full_command,
                                       read_dict, read_names, long_read_filename):
    alignment_dir = os.path.join(args.out, 'read_alignment')
    graph_fasta = os.path.join(alignment_dir, 'all_segments.fasta')
    anchor_segment_names = set(str(x.number) for x in anchor_segments)
    alignments_sam = os.path.join(alignment_dir, 'long_read_alignments.sam')
    scoring_scheme = AlignmentScoringScheme(args.scores)
    min_alignment_length = settings.MIN_LONG_READ_ALIGNMENT_LENGTH

    if not os.path.exists(alignment_dir):
        os.makedirs(alignment_dir)
    graph.save_to_fasta(graph_fasta, silent=True)
    references = load_references(graph_fasta, section_header=None, show_progress=False)
    reference_dict = {x.name: x for x in references}

    # Load existing alignments if available.
    if os.path.isfile(alignments_sam) and sam_references_match(alignments_sam, graph):
        log.log('\nSAM file already exists. Will use these alignments instead of conducting '
                'a new alignment:')
        log.log('  ' + alignments_sam)
        alignments = load_sam_alignments(alignments_sam, read_dict, reference_dict,
                                         scoring_scheme)
        for alignment in alignments:
            read_dict[alignment.read.name].alignments.append(alignment)
        print_alignment_summary_table(read_dict, args.verbosity, False)

    # Conduct the alignment if an existing SAM is not available.
    else:
        alignments_sam = os.path.join(alignment_dir, 'long_read_alignments.sam')
        alignments_in_progress = alignments_sam + '.incomplete'

        allowed_overlap = int(round(graph.overlap * settings.ALLOWED_ALIGNMENT_OVERLAP))
        low_score_threshold = [args.low_score]
        semi_global_align_long_reads(references, graph_fasta, read_dict, read_names,
                                     long_read_filename, args.threads, scoring_scheme,
                                     low_score_threshold, False, min_alignment_length,
                                     alignments_in_progress, full_command, allowed_overlap,
                                     0, args.contamination, args.verbosity,
                                     single_copy_segment_names=anchor_segment_names)
        shutil.move(alignments_in_progress, alignments_sam)

        if args.keep < 2:
            shutil.rmtree(alignment_dir, ignore_errors=True)
            log.log('\nDeleting ' + alignment_dir + '/')

    # Discard any reads that mostly align to known contamination.
    if args.contamination:
        filtered_read_names = []
        filtered_read_dict = {}
        contaminant_read_count = 0
        for read_name in read_names:
            if read_dict[read_name].mostly_aligns_to_contamination():
                contaminant_read_count += 1
            else:
                filtered_read_names.append(read_name)
                filtered_read_dict[read_name] = read_dict[read_name]
        read_names = filtered_read_names
        read_dict = filtered_read_dict
        log.log('\nDiscarded ' + str(contaminant_read_count) + ' reads as contamination', 2)

    # Use the long reads which aligned entirely within contigs (which are most likely correct)
    # to determine a minimum score.
    contained_reads = [x for x in read_dict.values() if x.has_one_contained_alignment()]
    contained_scores = []
    for read in contained_reads:
        contained_scores += [x.scaled_score for x in read.alignments]
    min_scaled_score = get_percentile(contained_scores, settings.MIN_SCALED_SCORE_PERCENTILE)

    log.log('\nSetting the minimum scaled score to the ' +
            float_to_str(settings.MIN_SCALED_SCORE_PERCENTILE, 1) +
            'th percentile of full read alignments: ' + float_to_str(min_scaled_score, 2), 2)

    return read_names, min_scaled_score, min_alignment_length


def clean_up_spades_graph(graph):
    log.log_section_header('Cleaning graph')
    log.log_explanation('Unicycler now performs various cleaning procedures on the graph to '
                        'remove overlaps and simplify the graph structure. The end result is a '
                        'graph ready for bridging.', verbosity=1)
    graph.remove_all_overlaps()
    while True:
        graph.repair_multi_way_junctions()
        graph.remove_unnecessary_links()
        graph.expand_repeats()
        if not graph.remove_zero_length_segs():
            break
    while True:
        if not graph.merge_small_segments(5):
            break
    graph.normalise_read_depths()
    graph.renumber_segments()
    graph.sort_link_order()
