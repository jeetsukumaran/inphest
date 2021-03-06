#! /usr/bin/env python

import os
import sys
import argparse
try:
    from StringIO import StringIO # Python 2 legacy support: StringIO in this module is the one needed (not io)
except ImportError:
    from io import StringIO # Python 3
import inphest
# from inphest import simulate
# from inphest import model
from inphest import utility
from inphest import simulate
from inphest import model

def get_example_model():
    model_definition = {}
    return model_definition

def main():
    parser = argparse.ArgumentParser(
            description="{} Biogeographical Simulator".format(inphest.description())
            )
    host_options = parser.add_argument_group("Host Biogeography Options")
    host_options.add_argument("-H", "--host-biogeographic-history",
            metavar="HOST-EVENT-FILE",
            default=None,
            help="Path to file providing the host biogeographic events.")
    host_options.add_argument("-F", "--host-biogeographic-history-format",
            choices=["revbayes", "archipelago"],
            default="revbayes",
            help="Format of the host biogeographic history.")

    model_options = parser.add_argument_group("Simulation Model")
    model_options.add_argument("model_file",
            nargs="?",
            metavar="MODEL-FILE",
            help="Path to file defining the model.")
    model_options.add_argument("-f", "--model-format",
            dest="model_file_schema",
            choices=["json", "python"],
            default=None,
            help="Format of model file.")
    model_options.add_argument("--create-example-model-file",
            metavar="FILEPATH.json",
            default=None,
            help="Create an example model file (in JSON format).")
    model_options.add_argument("--run-example-model",
            action="store_true",
            help="Run analysis under an example model.")

    output_options = parser.add_argument_group("Output Options")
    output_options.add_argument('-o', '--output-prefix',
        action='store',
        dest='output_prefix',
        type=str,
        default='inphest',
        metavar='OUTPUT-FILE-PREFIX',
        help="Prefix for output files (default: '%(default)s').")

    run_options = parser.add_argument_group("Run Options")
    run_options.add_argument("-n", "--nreps",
            type=int,
            default=10,
            help="Number of replicates (default: %(default)s).")
    run_options.add_argument("-z", "--random-seed",
            default=None,
            help="Seed for random number generator engine.")
    run_options.add_argument("--log-frequency",
            default=None,
            type=float,
            help="Frequency that background progress messages get written to the log (0: do not log informational messages).")
    run_options.add_argument("--file-logging-level",
            default="info",
            help="Message level threshold for file logs.")
    run_options.add_argument("--stderr-logging-level",
            default="info",
            help="Message level threshold for screen logs.")
    run_options.add_argument("--debug-mode",
            action="store_true",
            default=False,
            help="Run in debugging mode.")

    args = parser.parse_args()
    if args.host_biogeographic_history is None and not args.create_example_model_file:
        sys.exit("Require path to host biogeographic history events to be specified.")

    config_d = {}
    if args.model_file is None:
        if args.run_example_model:
            model_definition_source = get_example_model()
            model_definition_type = "python-dict"
            interpolate_missing_model_values = True
        elif args.create_example_model_file:
            run_logger = utility.RunLogger(
                    name="inphest",
                    stderr_logging_level=args.stderr_logging_level,
                    log_to_file=False,
                    log_path="dummy",
                    file_logging_level="error",
                    )
            example_model = model.InphestModel.from_definition_dict(
                model_definition=get_example_model(),
                interpolate_missing_model_values=True,
                run_logger=run_logger)
            if args.create_example_model_file == "-":
                out = sys.stdout
            else:
                out = open(os.path.expanduser(os.path.expandvars(args.create_example_model_file)), "w")
            example_model.write_model(out)
            sys.exit(0)
        else:
            sys.exit("Need to specify path to model specification file.\n"
                     "Use option '--create-example-model-file' to generate an example model definition file.\n"
                     "Use option '--run-example-model' to run the simulation under the example model."
                    )
    else:
        if args.run_example_model:
            sys.exit("Cannot run example model and specified model file ('{}') at the same time".format(args.model_file))
        if args.create_example_model_file:
            sys.exit("Cannot create example model and run specified model file ('{}') at the same time".format(args.model_file))
        model_definition_source = args.model_file
        if args.model_file_schema == "json":
            model_definition_type = "json-filepath"
        elif args.model_file_schema == "python":
            model_definition_type = "python-dict-filepath"
        else:
            ext = os.path.splitext(args.model_file)[1]
            if ext ==  ".json":
                model_definition_type = "json-filepath"
            elif ext == ".py":
                model_definition_type = "python-dict-filepath"
            else:
                sys.exit("Model definition format cannot be diagnosed from extension. Need to specify '--model-format'.")
        interpolate_missing_model_values = False

    # rb_data = os.path.join(utility.TEST_DATA_PATH, "revbayes", "bg_large.events.txt")
    simulate.repeat_run(
            output_prefix=args.output_prefix,
            nreps=args.nreps,
            host_history_samples_path=args.host_biogeographic_history,
            host_history_samples_format=args.host_biogeographic_history_format,
            model_definition_source=model_definition_source,
            model_definition_type=model_definition_type,
            interpolate_missing_model_values=interpolate_missing_model_values,
            config_d=config_d,
            random_seed=args.random_seed,
            stderr_logging_level=args.stderr_logging_level,
            file_logging_level=args.file_logging_level,
            debug_mode=args.debug_mode)

if __name__ == "__main__":
    main()

